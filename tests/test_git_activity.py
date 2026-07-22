from __future__ import annotations

import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client
from git import Repo

from mcp_agent_mail import app
from mcp_agent_mail.models import Project


def test_latest_git_activity_uses_one_bounded_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="1720000000\n", stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    result = app._latest_git_activity(Repo.init(tmp_path), ":(glob)**/*.py")

    assert result == datetime.fromtimestamp(1720000000, tz=timezone.utc)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[-2:] == ["--", ":(glob)**/*.py"]
    assert kwargs["timeout"] == app._GIT_ACTIVITY_TIMEOUT_SECONDS


def test_latest_git_activity_treats_timeout_as_missing_activity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def timed_out(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, cast(float, kwargs["timeout"]))

    monkeypatch.setattr(app.subprocess, "run", timed_out)

    result = app._latest_git_activity(
        Repo.init(tmp_path),
        "slow.py",
    )

    assert result is None


@pytest.mark.asyncio
async def test_reservation_activity_probe_runs_off_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def record_thread(
        workspace: object,
        repo: object,
        pattern: object,
        *,
        recent_after: object,
    ) -> tuple[bool, None, None]:
        worker_threads.append(threading.get_ident())
        return False, None, None

    monkeypatch.setattr(app, "_compute_reservation_activity", record_thread)

    await app._compute_reservation_activity_async(
        tmp_path,
        None,
        "file.py",
        recent_after=None,
    )

    assert worker_threads
    assert worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_file_reservations_resource_skips_git_history_scan(
    monkeypatch: pytest.MonkeyPatch, isolated_env: object
) -> None:
    original = app._collect_file_reservation_statuses
    include_git_activity_values: list[bool] = []

    async def record_collection(
        project: Project,
        *,
        include_released: bool = False,
        now: datetime | None = None,
        include_git_activity: bool = True,
    ) -> object:
        include_git_activity_values.append(include_git_activity)
        return await original(
            project,
            include_released=include_released,
            now=now,
            include_git_activity=include_git_activity,
        )

    monkeypatch.setattr(app, "_collect_file_reservation_statuses", record_collection)

    server = app.build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/git-scan-regression"})
        await client.read_resource("resource://file_reservations/git-scan-regression?active_only=false")

    assert include_git_activity_values
    assert not any(include_git_activity_values)


@pytest.mark.asyncio
async def test_historical_file_reservations_skip_live_filesystem_scan(
    monkeypatch: pytest.MonkeyPatch, isolated_env: object, tmp_path: Path
) -> None:
    server = app.build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/released-scan-regression"})
        agent_result = await client.call_tool(
            "register_agent",
            {"project_key": "released-scan-regression", "program": "test", "model": "test"},
        )
        agent_name = agent_result.data["name"]
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "released-scan-regression",
                "agent_name": agent_name,
                "paths": ["released.py"],
            },
        )
        await client.call_tool(
            "release_file_reservations",
            {
                "project_key": "released-scan-regression",
                "agent_name": agent_name,
                "paths": ["released.py"],
            },
        )

        monkeypatch.setattr(app, "_project_workspace_path", lambda project: tmp_path)

        def unexpected_scan(base: Path, pattern: str) -> list[Path]:
            raise AssertionError(f"released reservation scanned filesystem: {base} {pattern}")

        monkeypatch.setattr(app, "_collect_matching_paths", unexpected_scan)
        await client.read_resource(
            "resource://file_reservations/released-scan-regression?active_only=false"
        )
