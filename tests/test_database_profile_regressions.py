from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import _automatic_mutation_keys, build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.models import (
    Agent,
    AuditEvent,
    FileReservation,
    IdempotencyRecord,
    Message,
    MessageRecipient,
    Project,
    RuntimeBinding,
    WindowIdentity,
)


def _configure_database_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    _config.clear_settings_cache()


async def _register_agent(
    client: Client,
    *,
    project_key: str,
    name: str,
) -> dict[str, Any]:
    result = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": "regression-test",
            "model": "test-model",
            "name": name,
        },
    )
    return dict(result.data)


def _rpc_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _jsonrpc_failed(payload: dict[str, Any]) -> bool:
    if payload.get("error"):
        return True
    result = payload.get("result")
    return isinstance(result, dict) and bool(result.get("isError"))


async def _fresh_http_tool_call(
    *,
    window_uuid: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with app.router.lifespan_context(app), AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_rpc_tool(tool_name, arguments),
        )
    return response.status_code, response.json()


async def _table_counts() -> tuple[int, int, int, int]:
    async with get_session() as session:
        return (
            int(await session.scalar(select(func.count()).select_from(Message)) or 0),
            int(
                await session.scalar(select(func.count()).select_from(MessageRecipient))
                or 0
            ),
            int(
                await session.scalar(select(func.count()).select_from(IdempotencyRecord))
                or 0
            ),
            int(await session.scalar(select(func.count()).select_from(AuditEvent)) or 0),
        )


@pytest.mark.asyncio
async def test_database_send_lock_timeout_has_no_late_commit_then_keyed_retry_replays_once(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database_profile(monkeypatch)
    project_key = "/regression/database-lock-timeout"
    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        sender = await _register_agent(client, project_key=project_key, name="BlueLake")
        recipient = await _register_agent(client, project_key=project_key, name="GreenCastle")
        arguments = {
            "project_key": project_key,
            "sender_name": sender["name"],
            "sender_token": sender["registration_token"],
            "to": [recipient["name"]],
            "subject": "Lock timeout must roll back",
            "body_md": "A timed-out write must never commit after its caller sees failure.",
            "idempotency_key": "lock-timeout-send-v1",
        }
        before = await _table_counts()

        lock_connection = sqlite3.connect(
            str(tmp_path / "test.sqlite3"),
            isolation_level=None,
            timeout=0.1,
        )
        lock_connection.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            with pytest.raises(Exception) as failed:
                await asyncio.wait_for(
                    client.call_tool("send_message", arguments),
                    timeout=6.8,
                )
            assert not isinstance(failed.value, TimeoutError), (
                "database-profile send exceeded the client-side deadline instead of "
                "returning its bounded lock failure"
            )
        finally:
            elapsed = time.monotonic() - started
            lock_connection.rollback()
            lock_connection.close()

        assert elapsed < 7.0
        await asyncio.sleep(0.1)
        assert await _table_counts() == before

        committed = await client.call_tool("send_message", arguments)
        replayed = await client.call_tool("send_message", arguments)

    committed_payload = committed.data["deliveries"][0]["payload"]
    replayed_payload = replayed.data["deliveries"][0]["payload"]
    assert committed_payload["replayed"] is False
    assert replayed_payload["replayed"] is True
    assert replayed_payload["id"] == committed_payload["id"]
    after = await _table_counts()
    assert after == tuple(value + 1 for value in before)


@pytest.mark.asyncio
async def test_database_no_key_reserve_release_reacquire_leaves_live_reservation(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_database_profile(monkeypatch)
    project_key = "/regression/reservation-reacquire"
    path = "src/reacquire.py"
    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_agent(client, project_key=project_key, name="BlueLake")
        acquire_arguments = {
            "project_key": project_key,
            "agent_name": agent["name"],
            "registration_token": agent["registration_token"],
            "paths": [path],
            "ttl_seconds": 600,
        }
        first = await client.call_tool("file_reservation_paths", acquire_arguments)
        released = await client.call_tool(
            "release_file_reservations",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "registration_token": agent["registration_token"],
                "paths": [path],
            },
        )
        reacquired = await client.call_tool("file_reservation_paths", acquire_arguments)

    first_id = int(first.data["granted"][0]["id"])
    reacquired_id = int(reacquired.data["granted"][0]["id"])
    assert first.data["replayed"] is False
    assert released.data["replayed"] is False
    assert reacquired.data["replayed"] is False
    assert first.data["idempotency_mode"] == "state"
    assert released.data["idempotency_mode"] == "state"
    assert reacquired.data["idempotency_mode"] == "state"
    assert released.data["released"] == 1
    assert reacquired_id != first_id

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as session:
        rows = (
            await session.execute(
                select(FileReservation).where(
                    cast(Any, FileReservation.path_pattern) == path
                )
            )
        ).scalars().all()
    assert len(rows) == 2
    live = [row for row in rows if row.released_ts is None and row.expires_ts > now]
    assert [row.id for row in live] == [reacquired_id]


def test_automatic_message_keys_cover_retry_just_under_sixty_seconds() -> None:
    initial_time = datetime(2026, 7, 22, 12, 0, 29, 999_000, tzinfo=timezone.utc)
    retry_time = initial_time + timedelta(seconds=59, milliseconds=999)
    payload = {
        "sender_id": 7,
        "to": ["GreenCastle"],
        "subject": "Boundary retry",
        "body_md": "Same logical request after a lost response.",
    }

    initial_keys = _automatic_mutation_keys("message_send_v1", payload, now=initial_time)
    retry_keys = _automatic_mutation_keys("message_send_v1", payload, now=retry_time)

    assert initial_keys[0] == retry_keys[2]
    assert initial_keys[0] in retry_keys


@pytest.mark.asyncio
async def test_database_concurrent_first_registration_claims_one_window_once(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_database_profile(monkeypatch)
    window_uuid = str(uuid.uuid4())
    project_key = "/regression/concurrent-window-registration"
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    _config.clear_settings_cache()

    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        arguments = {
            "project_key": project_key,
            "program": "regression-test",
            "model": "test-model",
        }
        first, second = await asyncio.gather(
            client.call_tool("register_agent", arguments),
            client.call_tool("register_agent", arguments),
        )

    assert first.data["id"] == second.data["id"]
    assert first.data["name"] == second.data["name"]
    assert first.data["registration_token"] == second.data["registration_token"]
    async with get_session() as session:
        agents = (await session.execute(select(Agent))).scalars().all()
        bindings = (await session.execute(select(WindowIdentity))).scalars().all()
    assert len(agents) == 1
    assert len(bindings) == 1
    assert bindings[0].window_uuid == window_uuid
    assert bindings[0].agent_id == agents[0].id
    assert bindings[0].display_name == agents[0].name == first.data["name"]


@pytest.mark.asyncio
async def test_runtime_binding_reconcile_is_generation_fenced_and_requires_route_readback(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_database_profile(monkeypatch)
    owner_token = "test-runtime-controller-owner"
    monkeypatch.setenv("CORE_OWNER_TOKEN", owner_token)
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    _config.clear_settings_cache()
    project_key = "/regression/runtime-binding-generation"
    runtime_session_id = str(uuid.uuid4())
    pane_incarnation_id = str(uuid.uuid4())
    started = "2026-07-22T15:00:00Z"

    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_agent(client, project_key=project_key, name="BlueLake")
        before = await client.call_tool(
            "identity_status",
            {"project_key": project_key, "agent_name": agent["name"]},
        )
        assert before.data["overall"] == "runtime_missing"

        reconcile_args = {
            "project_key": project_key,
            "agent_name": agent["name"],
            "registration_token": agent["registration_token"],
            "owner_token": owner_token,
            "runtime_session_id": runtime_session_id,
            "pane_incarnation_id": pane_incarnation_id,
            "host_id": "host-a",
            "tmux_server_id": "server-a",
            "pane_id": "%24",
            "program": "codex",
            "model": "gpt-5.6",
            "process_started_ts": started,
            "expected_generation": 0,
        }
        enrolled = await client.call_tool("reconcile_runtime_binding", reconcile_args)
        assert enrolled.data["generation"] == 1
        assert enrolled.data["overall"] == "route_missing"

        ready = await client.call_tool(
            "identity_status",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "host_id": "host-a",
                "tmux_server_id": "server-a",
                "pane_id": "%24",
                "route_generation": 1,
            },
        )
        assert ready.data["overall"] == "ready"
        assert ready.data["route"]["status"] == "verified"

        moved_args = dict(reconcile_args)
        moved_args.update({"pane_id": "%31", "expected_generation": 1})
        moved = await client.call_tool("reconcile_runtime_binding", moved_args)
        assert moved.data["generation"] == 2

        stale_args = dict(moved_args)
        stale_args["pane_id"] = "%32"
        with pytest.raises(ToolError, match="generation changed"):
            await client.call_tool("reconcile_runtime_binding", stale_args)

        conflict_args = dict(moved_args)
        conflict_args.update(
            {
                "pane_incarnation_id": str(uuid.uuid4()),
                "expected_generation": 2,
            }
        )
        with pytest.raises(ToolError, match="another active runtime incarnation"):
            await client.call_tool("reconcile_runtime_binding", conflict_args)

        issued = await client.call_tool(
            "issue_continuation_receipt",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "registration_token": agent["registration_token"],
                "owner_token": owner_token,
                "expected_generation": 2,
            },
        )
        receipt = issued.data["continuation_receipt"]
        continued = await client.call_tool(
            "consume_continuation_receipt",
            {
                "project_key": project_key,
                "continuation_receipt": receipt,
                "host_id": "host-a",
                "tmux_server_id": "server-a",
                "pane_id": "%40",
                "program": "codex",
                "model": "gpt-5.6-new-account",
                "process_started_ts": "2026-07-22T15:10:00Z",
            },
        )
        assert continued.data["generation"] == 3
        assert continued.data["runtime_session_id"] == runtime_session_id
        assert continued.data["pane_incarnation_id"] == pane_incarnation_id
        with pytest.raises(ToolError, match="consumed or expired"):
            await client.call_tool(
                "consume_continuation_receipt",
                {
                    "project_key": project_key,
                    "continuation_receipt": receipt,
                    "host_id": "host-a",
                    "tmux_server_id": "server-a",
                    "pane_id": "%40",
                    "program": "codex",
                    "model": "gpt-5.6-new-account",
                    "process_started_ts": "2026-07-22T15:10:00Z",
                },
            )

        new_window_uuid = str(uuid.uuid4())
        rotated = await client.call_tool(
            "rotate_runtime_binding",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "registration_token": agent["registration_token"],
                "owner_token": owner_token,
                "expected_generation": 3,
                "new_window_uuid": new_window_uuid,
                "runtime_session_id": str(uuid.uuid4()),
                "pane_incarnation_id": str(uuid.uuid4()),
                "host_id": "host-a",
                "tmux_server_id": "server-a",
                "pane_id": "%40",
                "program": "codex",
                "model": "gpt-5.6-new-task",
                "process_started_ts": "2026-07-22T15:20:00Z",
            },
        )
        assert rotated.data["generation"] == 4

    async with get_session() as session:
        rows = (
            await session.execute(
                select(RuntimeBinding).order_by(cast(Any, RuntimeBinding.generation))
            )
        ).scalars().all()
    assert [(row.generation, row.state) for row in rows] == [
        (1, "ended"),
        (2, "ended"),
        (3, "ended"),
        (4, "healthy"),
    ]

    old_status, old_payload = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": agent["name"],
            "include_recent_commits": False,
        },
    )
    assert old_status == 200
    assert _jsonrpc_failed(old_payload)
    new_status, new_payload = await _fresh_http_tool_call(
        window_uuid=new_window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": agent["name"],
            "include_recent_commits": False,
        },
    )
    assert new_status == 200
    assert not _jsonrpc_failed(new_payload)


@pytest.mark.asyncio
async def test_managed_watcher_v2_signal_and_prestop_are_durable_bounded_and_fail_open(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database_profile(monkeypatch)
    owner_token = "test-managed-watcher-owner"
    monkeypatch.setenv("CORE_OWNER_TOKEN", owner_token)
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    signals_dir = tmp_path / "signals"
    monkeypatch.setenv("NOTIFICATIONS_SIGNALS_DIR", str(signals_dir))
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    _config.clear_settings_cache()
    project_key = "/regression/managed-watcher"

    async with Client(build_mcp_server()) as setup_client:
        await setup_client.call_tool("ensure_project", {"human_key": project_key})
        sender = await _register_agent(
            setup_client,
            project_key=project_key,
            name="GreenCastle",
        )

    window_uuid = str(uuid.uuid4())
    runtime_session_id = str(uuid.uuid4())
    pane_incarnation_id = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    _config.clear_settings_cache()
    async with Client(build_mcp_server()) as client:
        recipient = await _register_agent(client, project_key=project_key, name="BlueLake")
        await client.call_tool(
            "set_contact_policy",
            {"project_key": project_key, "agent_name": recipient["name"], "policy": "open"},
        )
        await client.call_tool(
            "reconcile_runtime_binding",
            {
                "project_key": project_key,
                "agent_name": recipient["name"],
                "registration_token": recipient["registration_token"],
                "owner_token": owner_token,
                "runtime_session_id": runtime_session_id,
                "pane_incarnation_id": pane_incarnation_id,
                "host_id": "host-watcher",
                "tmux_server_id": "server-watcher",
                "pane_id": "%41",
                "program": "codex",
                "model": "gpt-5.6",
                "process_started_ts": "2026-07-22T15:00:00Z",
                "expected_generation": 0,
            },
        )
        sent = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": sender["name"],
                "sender_token": sender["registration_token"],
                "to": [recipient["name"]],
                "subject": "ACK me",
                "body_md": "This body must never enter the control envelope.",
                "importance": "urgent",
                "ack_required": True,
            },
        )
        message_id = int(sent.data["deliveries"][0]["payload"]["id"])

        staged = await client.call_tool(
            "watch_inbox",
            {
                "project_key": project_key,
                "agent_name": recipient["name"],
                "runtime_session_id": runtime_session_id,
            },
        )
        assert [item["id"] for item in staged.data["messages"]] == [message_id]
        obligation_id = staged.data["messages"][0]["recipient_obligation_id"]
        assert staged.data["messages"][0]["recipient_provenance"] == "explicit_to"
        replayed = await client.call_tool(
            "watch_inbox",
            {
                "project_key": project_key,
                "agent_name": recipient["name"],
                "runtime_session_id": runtime_session_id,
            },
        )
        assert replayed.data["replayed"] is True
        assert replayed.data["discovery_generation"] == staged.data["discovery_generation"]
        committed = await client.call_tool(
            "commit_inbox_discovery",
            {
                "project_key": project_key,
                "agent_name": recipient["name"],
                "runtime_session_id": runtime_session_id,
                "discovery_generation": staged.data["discovery_generation"],
            },
        )
        assert committed.data["cursor"] == message_id
        assert committed.data["marked_read"] is False

    async with get_session() as session:
        project = (await session.execute(select(Project))).scalar_one()
        recipient_row = (
            await session.execute(
                select(Agent).where(cast(Any, Agent.name) == recipient["name"])
            )
        ).scalar_one()
    signal_path = (
        signals_dir
        / "v2"
        / "projects"
        / str(project.id)
        / "agents"
        / f"{recipient_row.id}.signal"
    )
    signal = json.loads(signal_path.read_text(encoding="utf-8"))
    assert signal["schema_version"] == 2
    assert signal["recipient_agent_id"] == recipient_row.id
    assert signal["obligations"][0]["recipient_obligation_id"] == obligation_id
    assert "subject" not in json.dumps(signal)
    assert "body" not in json.dumps(signal)

    decision_args = {
        "project_key": project_key,
        "agent_name": recipient["name"],
        "runtime_session_id": runtime_session_id,
        "pane_incarnation_id": pane_incarnation_id,
        "runtime_generation": 1,
        "host_id": "host-watcher",
        "tmux_server_id": "server-watcher",
        "pane_id": "%41",
        "signal_project_id": project.id,
        "signal_recipient_agent_id": recipient_row.id,
        "signal_generation": signal["generation"],
        "recipient_obligation_id": obligation_id,
    }
    status, first = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="pre_stop_decision",
        arguments=decision_args,
    )
    assert status == 200
    assert first["result"]["structuredContent"]["decision"] == "block_stop"

    _, second = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="pre_stop_decision",
        arguments=decision_args,
    )
    assert second["result"]["structuredContent"]["attempt"] == 2
    _, exhausted = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="pre_stop_decision",
        arguments=decision_args,
    )
    assert exhausted["result"]["structuredContent"]["decision"] == "allow_stop"
    assert exhausted["result"]["structuredContent"]["reason_code"] == "stop_budget_exhausted"

    wrong_route = dict(decision_args)
    wrong_route["pane_id"] = "%wrong"
    _, deferred = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="pre_stop_decision",
        arguments=wrong_route,
    )
    assert deferred["result"]["structuredContent"] == {
        "decision": "allow_stop",
        "reason_code": "notification_deferred",
        "diagnostic": "notification_deferred",
    }

    _, marked = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="mark_message_read",
        arguments={
            "project_key": project_key,
            "agent_name": recipient["name"],
            "message_id": message_id,
        },
    )
    assert not _jsonrpc_failed(marked)
    _, after_read = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="pre_stop_decision",
        arguments=decision_args,
    )
    assert after_read["result"]["structuredContent"]["decision"] == "allow_stop"
    assert after_read["result"]["structuredContent"]["reason_code"] == "no_blocking_obligation"


@pytest.mark.asyncio
async def test_expired_exact_window_binding_renews_with_valid_registration_token(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_database_profile(monkeypatch)
    window_uuid = str(uuid.uuid4())
    project_key = "/regression/expired-window-renewal"
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    _config.clear_settings_cache()
    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_agent(client, project_key=project_key, name="BlueLake")

    expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    async with get_session() as session:
        binding = (await session.execute(select(WindowIdentity))).scalar_one()
        binding.expires_ts = expired_at
        session.add(binding)
        await session.commit()

    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    _config.clear_settings_cache()
    status, payload = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": agent["name"],
            "registration_token": agent["registration_token"],
            "include_recent_commits": False,
        },
    )

    assert status == 200
    assert not _jsonrpc_failed(payload)
    async with get_session() as session:
        renewed = (await session.execute(select(WindowIdentity))).scalar_one()
    assert renewed.display_name == agent["name"]
    assert renewed.expires_ts is not None
    assert renewed.expires_ts > datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_omitted_sender_invalid_token_cannot_fall_through_window_binding(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_database_profile(monkeypatch)
    window_uuid = str(uuid.uuid4())
    project_key = "/regression/omitted-sender-invalid-token"
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    _config.clear_settings_cache()
    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        bound = await _register_agent(client, project_key=project_key, name="BlueLake")

    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    _config.clear_settings_cache()
    async with Client(build_mcp_server()) as client:
        other = await _register_agent(client, project_key=project_key, name="RedStone")
        original = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": other["name"],
                "sender_token": other["registration_token"],
                "to": [bound["name"]],
                "subject": "Original",
                "body_md": "Only the bound agent may reply.",
                "idempotency_key": "invalid-token-original-v1",
            },
        )
    original_id = int(original.data["deliveries"][0]["payload"]["id"])

    status, payload = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="reply_message",
        arguments={
            "project_key": project_key,
            "message_id": original_id,
            "body_md": "This must not authenticate through the window mapping.",
            "registration_token": "explicitly-invalid-token",
            "idempotency_key": "invalid-token-reply-v1",
        },
    )

    assert status == 200
    assert _jsonrpc_failed(payload)
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 1


@pytest.mark.asyncio
async def test_database_exact_window_recovers_legacy_auto_retired_agent_without_token(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_database_profile(monkeypatch)
    window_uuid = str(uuid.uuid4())
    project_key = "/regression/auto-retired-window-recovery"
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    _config.clear_settings_cache()
    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_agent(client, project_key=project_key, name="BlueLake")

    retired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    last_active = retired_at - timedelta(days=1, seconds=1)
    async with get_session() as session:
        db_agent = await session.get(Agent, int(agent["id"]))
        assert db_agent is not None
        db_agent.last_active_ts = last_active
        db_agent.retired_at = retired_at
        await session.commit()

    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    _config.clear_settings_cache()
    status, payload = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": agent["name"],
            "include_recent_commits": False,
        },
    )

    assert status == 200
    assert not _jsonrpc_failed(payload)
    async with get_session() as session:
        recovered = await session.get(Agent, int(agent["id"]))
        assert recovered is not None
        assert recovered.retired_at is None
        recovery_events = await session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.operation_kind == "agent_auto_retire_recovered_v1"
            )
        )
    assert recovery_events == 1


@pytest.mark.asyncio
async def test_database_http_lifespan_does_not_start_legacy_auto_retire_worker(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_database_profile(monkeypatch)
    monkeypatch.setenv("AUTO_RETIRE_STALE_AGENTS_ENABLED", "true")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    async with app.router.lifespan_context(app):
        task_names = {
            task.get_coro().__qualname__
            for task in app.state._background_tasks
        }
        assert not any("auto_retire" in name for name in task_names)


@pytest.mark.asyncio
async def test_database_exact_window_does_not_recover_explicitly_retired_agent(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_database_profile(monkeypatch)
    window_uuid = str(uuid.uuid4())
    project_key = "/regression/explicit-retirement-stays-retired"
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    _config.clear_settings_cache()
    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_agent(client, project_key=project_key, name="BlueLake")
        await client.call_tool(
            "retire_agent",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "registration_token": agent["registration_token"],
            },
        )

    # Make the timestamps look sweep-stale so the explicit retirement audit is
    # the fact that prevents compatibility recovery.
    async with get_session() as session:
        db_agent = await session.get(Agent, int(agent["id"]))
        assert db_agent is not None
        assert db_agent.retired_at is not None
        db_agent.last_active_ts = db_agent.retired_at - timedelta(days=1, seconds=1)
        await session.commit()

    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    _config.clear_settings_cache()
    status, payload = await _fresh_http_tool_call(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": agent["name"],
            "include_recent_commits": False,
        },
    )

    assert status == 200
    assert _jsonrpc_failed(payload)
    async with get_session() as session:
        still_retired = await session.get(Agent, int(agent["id"]))
        assert still_retired is not None
        assert still_retired.retired_at is not None
        recovery_events = await session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.operation_kind == "agent_auto_retire_recovered_v1"
            )
        )
    assert recovery_events == 0
