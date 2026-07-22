from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_gitpython_is_not_a_core_dependency() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    assert not any(dependency.lower().startswith("gitpython") for dependency in dependencies)
    assert any(
        dependency.lower().startswith("gitpython")
        for dependency in pyproject["project"]["optional-dependencies"]["legacy-git"]
    )


def test_core_server_imports_and_builds_when_gitpython_is_blocked(tmp_path: Path) -> None:
    script = r'''
import importlib.abc
import asyncio
import os
import sys

from httpx import ASGITransport, AsyncClient

class BlockGit(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "git" or fullname.startswith("git."):
            raise ImportError("GitPython intentionally unavailable")
        return None

sys.meta_path.insert(0, BlockGit())
for name in tuple(sys.modules):
    if name == "git" or name.startswith("git."):
        del sys.modules[name]
os.environ["PATH"] = ""
os.environ["RUNTIME_PROFILE"] = "core"
os.environ["CORE_OWNER_TOKEN"] = "owner-secret"
os.environ["CREDENTIAL_PEPPERS_JSON"] = '{"core":"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE"}'
os.environ["CREDENTIAL_CURRENT_PEPPER_KEY_ID"] = "core"
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.legacy_adapter import LegacyStorageUnavailableError, clear_repo_cache
server = build_mcp_server()
assert server is not None
http_app = build_http_app(get_settings(), server)
assert http_app is not None

async def exercise_lifecycle():
    async with http_app.router.lifespan_context(http_app):
        transport = ASGITransport(app=http_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/liveness")
            assert response.status_code == 200
            response = await client.get("/mail/archive")
            assert response.status_code == 409
            response = await client.post("/mail/api/delete-messages", json={"message_ids": [1]})
            assert response.status_code == 409
        await asyncio.sleep(0)

asyncio.run(exercise_lifecycle())
try:
    clear_repo_cache()
except LegacyStorageUnavailableError:
    pass
else:
    raise AssertionError("core runtime crossed the legacy storage boundary")
assert not any(name == "git" or name.startswith("git.") for name in sys.modules)
assert "mcp_agent_mail.storage" not in sys.modules
'''
    environment = dict(os.environ)
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'core.sqlite3'}"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
