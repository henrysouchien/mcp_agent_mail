import asyncio
import contextlib
import gc
import os
import secrets
import tempfile
from pathlib import Path

import psutil
import pytest

# Install the process-wide test isolation envelope before importing any Agent
# Mail module. Child processes inherit these values, so a test that spawns the
# real CLI cannot silently reopen the operator's production database or state
# directories during collection or execution.
_PYTEST_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="mcp-agent-mail-pytest-"))
os.environ["MCP_AGENT_MAIL_TEST_RUN_ID"] = secrets.token_hex(16)
os.environ["MCP_AGENT_MAIL_TEST_ROOT"] = str(_PYTEST_SESSION_ROOT)
os.environ["APP_ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_PYTEST_SESSION_ROOT / 'storage.sqlite3'}"
os.environ["STORAGE_ROOT"] = str(_PYTEST_SESSION_ROOT / "storage")
os.environ["BLOB_STORAGE_ROOT"] = str(_PYTEST_SESSION_ROOT / "blobs")
os.environ["NOTIFICATIONS_SIGNALS_DIR"] = str(_PYTEST_SESSION_ROOT / "signals")

from mcp_agent_mail.config import clear_settings_cache  # noqa: E402
from mcp_agent_mail.db import reset_database_state  # noqa: E402
from mcp_agent_mail.storage import clear_repo_cache  # noqa: E402

# CPU overload threshold - skip benchmark tests if ALL cores are at this level
CPU_OVERLOAD_THRESHOLD = 95.0


def is_cpu_overloaded() -> bool:
    """Check if all CPU cores are at 95%+ utilization.

    Returns True only when the system is under extreme load (all cores saturated),
    which would make timing-based benchmark tests unreliable.
    """
    # Sample CPU usage over 200ms per-core
    per_cpu = psutil.cpu_percent(interval=0.2, percpu=True)
    if not per_cpu:
        return False

    overloaded = sum(1 for usage in per_cpu if usage >= CPU_OVERLOAD_THRESHOLD)
    return overloaded == len(per_cpu)


def skip_if_cpu_overloaded() -> None:
    """Skip the current test if all CPU cores are at 95%+ utilization.

    Use this at the start of any test that asserts on wall-clock time.
    Prevents flaky benchmark tests when the system is under extreme load.
    """
    if is_cpu_overloaded():
        cores = psutil.cpu_count()
        pytest.skip(
            f"Skipping benchmark: system under extreme CPU load "
            f"(all {cores} cores at {CPU_OVERLOAD_THRESHOLD}%+ utilization)"
        )


@pytest.fixture(scope="function")
def event_loop():
    """Create a new event loop for each test function.

    This fixture ensures proper event loop cleanup on all platforms,
    particularly macOS where the default event loop policy can cause
    'Event loop is closed' errors if not handled properly.

    The fixture:
    1. Creates a fresh event loop for each test
    2. Properly shuts down async generators
    3. Cancels any pending tasks
    4. Closes the loop cleanly

    Note: In Python 3.14+, event loop policy management is deprecated.
    asyncio.new_event_loop() creates the appropriate loop type automatically.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    yield loop

    # Proper cleanup sequence
    try:
        # Cancel all pending tasks
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()

        # Allow cancelled tasks to complete
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

        # Shutdown async generators (Python 3.6+)
        loop.run_until_complete(loop.shutdown_asyncgens())

        # Shutdown default executor (Python 3.9+)
        if hasattr(loop, "shutdown_default_executor"):
            loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:
        pass  # Ignore cleanup errors
    finally:
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Provide isolated database settings for tests and reset caches."""
    db_path: Path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.setenv("MCP_AGENT_MAIL_TEST_ROOT", str(tmp_path))
    # Preserve the historical Git-archive contract for existing tests. Tests
    # of the production default remove this override explicitly.
    monkeypatch.setenv("RUNTIME_PROFILE", "legacy")
    # Host-level Agent Mail settings must not leak identities, notifications,
    # or tool filtering into isolated server tests.
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("TOOLS_FILTER_ENABLED", "false")
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("BLOB_STORAGE_ROOT", str(tmp_path / "blobs"))
    monkeypatch.setenv("NOTIFICATIONS_SIGNALS_DIR", str(tmp_path / "signals"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test-agent")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "128")
    clear_settings_cache()
    reset_database_state()
    # Clear repo cache before test to ensure isolation
    clear_repo_cache()
    try:
        yield
    finally:
        clear_repo_cache()
        reset_database_state()
        clear_settings_cache()


@pytest.fixture(autouse=True)
def _global_resource_cleanup():
    """Best-effort global cleanup to avoid FD leaks under low ulimit.

    Some tests don't opt into `isolated_env` but still touch the global engine/repo cache.
    With RLIMIT_NOFILE=256 (common on macOS), a small amount of leakage can cascade into
    EMFILE failures later in the suite.
    """
    yield

    # Close cached repo handles first.
    with contextlib.suppress(Exception):
        clear_repo_cache()

    # Dispose engine/pool state across tests.
    with contextlib.suppress(Exception):
        reset_database_state()

    with contextlib.suppress(Exception):
        clear_settings_cache()

    # Extra safety: close any Repo objects that escaped caching.
    with contextlib.suppress(Exception):
        from git import Repo

        gc.collect()
        for obj in gc.get_objects():
            if isinstance(obj, Repo):
                with contextlib.suppress(Exception):
                    obj.close()
