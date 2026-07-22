from __future__ import annotations

import base64
import contextlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from authlib.jose import JsonWebKey, jwt
from fastmcp import Client
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.http import build_http_app


def _rpc(method: str, params: dict) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


def _tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


async def _create_window_bound_agent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_key: str,
    agent_name: str,
    window_uuid: str,
) -> str:
    result = await _create_window_bound_registration(
        monkeypatch,
        project_key=project_key,
        agent_name=agent_name,
        window_uuid=window_uuid,
    )
    return str(result["name"])


async def _create_window_bound_registration(
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_key: str,
    agent_name: str,
    window_uuid: str,
) -> dict[str, Any]:
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "program": "codex",
            "model": "test-model",
            "task_description": "window identity http auth test",
        }
        if agent_name:
            arguments["name"] = agent_name
        result = await client.call_tool("register_agent", arguments)
    monkeypatch.delenv("MCP_AGENT_MAIL_WINDOW_ID", raising=False)
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    return dict(result.data)


async def _create_registered_agent(
    *,
    project_key: str,
    agent_name: str = "",
) -> tuple[str, str]:
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "program": "codex",
            "model": "test-model",
            "task_description": "http token-backed auth test",
        }
        if agent_name:
            arguments["name"] = agent_name
        result = await client.call_tool("register_agent", arguments)
    return str(result.data["name"]), str(result.data["registration_token"])


def _jsonrpc_failed(payload: dict[str, Any]) -> bool:
    if payload.get("error"):
        return True
    result = payload.get("result")
    return isinstance(result, dict) and bool(result.get("isError"))


def _assert_http_or_tool_rejected(status_code: int, payload: dict[str, Any]) -> None:
    """Accept rejection by the HTTP gate or by Agent Mail tool authentication."""
    assert status_code in {200, 401}
    if status_code == 401:
        assert payload.get("detail") == "Unauthorized"
    else:
        assert _jsonrpc_failed(payload)


async def _call_tool_over_fresh_http(
    *,
    window_uuid: str,
    tool_name: str,
    arguments: dict[str, Any],
    client_host: str = "127.0.0.1",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Call one tool through a newly built stateless HTTP server."""
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app, client=(client_host, 12345))
    headers = {"Authorization": f"Bearer mcp-window:{window_uuid}"}
    headers.update(extra_headers or {})
    async with app.router.lifespan_context(app), AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            settings.http.path,
            headers=headers,
            json=_tool(tool_name, arguments),
        )
    return response.status_code, response.json()


@pytest.mark.asyncio
async def test_http_bearer_and_cors_preflight(isolated_env, monkeypatch):
    # Enable Bearer and CORS
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "token123")
    monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
    monkeypatch.setenv("HTTP_CORS_ORIGINS", "http://example.com")
    # Disable localhost auto-authentication to properly test bearer auth
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Preflight OPTIONS
        r0 = await client.options(settings.http.path, headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
        })
        assert r0.status_code in (200, 204)
        # No bearer -> 401
        r1 = await client.post(settings.http.path, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r1.status_code == 401
        # With bearer
        r2 = await client.post(
            settings.http.path,
            headers={"Authorization": "Bearer token123", "Origin": "http://example.com"},
            json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
        )
        assert r2.status_code == 200
        # CORS header present on response
        assert r2.headers.get("access-control-allow-origin") in ("*", "http://example.com")


@pytest.mark.asyncio
async def test_http_window_identity_header_routes_but_does_not_authenticate(isolated_env, monkeypatch):
    project_key = "/test/http-window-header"
    agent_name = ""
    window_uuid = str(uuid.uuid4())
    agent_name = await _create_window_bound_agent(
        monkeypatch,
        project_key=project_key,
        agent_name=agent_name,
        window_uuid=window_uuid,
    )

    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        no_header = await client.post(
            settings.http.path,
            json=_tool("whois", {"project_key": project_key, "agent_name": agent_name}),
        )
        assert no_header.status_code == 200
        assert _jsonrpc_failed(no_header.json())

        with_header = await client.post(
            settings.http.path,
            headers={"X-MCP-Agent-Mail-Window-ID": window_uuid},
            json=_tool("whois", {"project_key": project_key, "agent_name": agent_name}),
        )
        assert with_header.status_code == 200
        assert _jsonrpc_failed(with_header.json())


@pytest.mark.asyncio
async def test_http_pane_credential_header_authenticates_without_tool_secret(
    isolated_env,
    monkeypatch,
) -> None:
    project_key = "/test/http-pane-carrier"
    window_uuid = str(uuid.uuid4())
    encoded_pepper = base64.urlsafe_b64encode(b"h" * 32).rstrip(b"=").decode("ascii")
    monkeypatch.setenv("CREDENTIAL_PEPPERS_JSON", json.dumps({"http-test": encoded_pepper}))
    monkeypatch.setenv("CREDENTIAL_CURRENT_PEPPER_KEY_ID", "http-test")
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    _config.clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        sender = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "codex",
                "model": "test-model",
                "name": "BlueLake",
            },
        )
        recipient = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "codex",
                "model": "test-model",
                "name": "RedStone",
            },
        )
        await client.call_tool(
            "set_contact_policy",
            {
                "project_key": project_key,
                "agent_name": recipient.data["name"],
                "policy": "open",
            },
        )
    pane_credential = str(sender.data["pane_credential"])

    settings = _config.get_settings()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    arguments = {
        "project_key": project_key,
        "to": [recipient.data["name"]],
        "subject": "Persistent HTTP pane",
        "body_md": "Authenticated by a secret-bearing request carrier.",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated = await client.post(
            settings.http.path,
            json=_tool("send_message", arguments),
        )
        authenticated = await client.post(
            settings.http.path,
            headers={"X-MCP-Agent-Mail-Pane-Credential": pane_credential},
            json=_tool("send_message", arguments),
        )

    assert _jsonrpc_failed(unauthenticated.json())
    assert not _jsonrpc_failed(authenticated.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_profile", ["legacy", "database"])
async def test_http_window_identity_bearer_rebinds_exact_active_agent_after_restart(
    isolated_env,
    monkeypatch,
    runtime_profile: str,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", runtime_profile)
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    _config.clear_settings_cache()
    project_key = f"/test/http-window-bearer-{runtime_profile}"
    window_uuid = str(uuid.uuid4())
    agent_name = await _create_window_bound_agent(
        monkeypatch,
        project_key=project_key,
        agent_name="BlueLake",
        window_uuid=window_uuid,
    )

    settings = _config.get_settings()
    # A new MCP server models a daemon restart: no FastMCP session binding is
    # available, so the persisted project/window mapping must be consulted.
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool(
                "whois",
                {
                    "project_key": project_key,
                    "agent_name": agent_name,
                    "include_recent_commits": False,
                },
            ),
        )
        assert response.status_code == 200
        assert not _jsonrpc_failed(response.json())


@pytest.mark.asyncio
async def test_http_token_backed_whois_binds_unclaimed_window_for_future_rebind(isolated_env, monkeypatch):
    project_key = "/test/http-window-token-whois"
    agent_name, registration_token = await _create_registered_agent(project_key=project_key)
    window_uuid = str(uuid.uuid4())

    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        before = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool(
                "whois",
                {
                    "project_key": project_key,
                    "agent_name": agent_name,
                    "include_recent_commits": False,
                },
            ),
        )
        assert before.status_code == 200
        assert _jsonrpc_failed(before.json())

        token_backed = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool(
                "whois",
                {
                    "project_key": project_key,
                    "agent_name": agent_name,
                    "registration_token": registration_token,
                    "include_recent_commits": False,
                },
            ),
        )
        assert token_backed.status_code == 200
        assert not _jsonrpc_failed(token_backed.json())

    fresh_app = build_http_app(settings, build_mcp_server())
    fresh_transport = ASGITransport(app=fresh_app)
    async with AsyncClient(transport=fresh_transport, base_url="http://test") as client:
        after = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool(
                "whois",
                {
                    "project_key": project_key,
                    "agent_name": agent_name,
                    "include_recent_commits": False,
                },
            ),
        )
        assert after.status_code == 200
        assert not _jsonrpc_failed(after.json())


@pytest.mark.asyncio
async def test_http_token_backed_whois_does_not_overwrite_conflicting_window_identity(isolated_env, monkeypatch):
    project_key = "/test/http-window-token-conflict"
    window_uuid = str(uuid.uuid4())
    first_agent = await _create_window_bound_agent(
        monkeypatch,
        project_key=project_key,
        agent_name="",
        window_uuid=window_uuid,
    )
    second_agent, second_token = await _create_registered_agent(project_key=project_key)
    assert second_agent != first_agent

    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token_backed_conflict = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool(
                "whois",
                {
                    "project_key": project_key,
                    "agent_name": second_agent,
                    "registration_token": second_token,
                    "include_recent_commits": False,
                },
            ),
        )
        assert token_backed_conflict.status_code == 200
        assert not _jsonrpc_failed(token_backed_conflict.json())

    fresh_app = build_http_app(settings, build_mcp_server())
    fresh_transport = ASGITransport(app=fresh_app)
    async with AsyncClient(transport=fresh_transport, base_url="http://test") as client:
        first_still_bound = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool(
                "whois",
                {
                    "project_key": project_key,
                    "agent_name": first_agent,
                    "include_recent_commits": False,
                },
            ),
        )
        assert first_still_bound.status_code == 200
        assert not _jsonrpc_failed(first_still_bound.json())

        second_not_bound = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool(
                "whois",
                {
                    "project_key": project_key,
                    "agent_name": second_agent,
                    "include_recent_commits": False,
                },
            ),
        )
        assert second_not_bound.status_code == 200
        assert _jsonrpc_failed(second_not_bound.json())


@pytest.mark.asyncio
async def test_http_window_rebind_supports_explicit_send_and_omitted_reply_after_restart(
    isolated_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    _config.clear_settings_cache()
    project_key = "/test/http-window-omitted-sender"
    window_uuid = str(uuid.uuid4())
    bound = await _create_window_bound_registration(
        monkeypatch,
        project_key=project_key,
        agent_name="BlueLake",
        window_uuid=window_uuid,
    )
    other_name, other_token = await _create_registered_agent(
        project_key=project_key,
        agent_name="RedStone",
    )

    async with Client(build_mcp_server()) as client:
        for agent_name, registration_token in (
            (str(bound["name"]), str(bound["registration_token"])),
            (other_name, other_token),
        ):
            await client.call_tool(
                "set_contact_policy",
                {
                    "project_key": project_key,
                    "agent_name": agent_name,
                    "registration_token": registration_token,
                    "policy": "open",
                },
            )
        original = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": other_name,
                "sender_token": other_token,
                "to": [bound["name"]],
                "subject": "Original for window reply",
                "body_md": "The reply should recover its sender from the window mapping.",
                "idempotency_key": "http-window-original-v1",
            },
        )
    original_id = int(original.data["deliveries"][0]["payload"]["id"])

    explicit_status, explicit_payload = await _call_tool_over_fresh_http(
        window_uuid=window_uuid,
        tool_name="send_message",
        arguments={
            "project_key": project_key,
            "sender_name": bound["name"],
            "to": [other_name],
            "subject": "Explicit sender after restart",
            "body_md": "Authenticated by the persisted local window binding.",
            "idempotency_key": "http-window-explicit-v1",
        },
    )
    assert explicit_status == 200
    assert not _jsonrpc_failed(explicit_payload)

    # Build another server for the reply so success cannot depend on the
    # preceding HTTP request having populated an in-memory session binding.
    omitted_status, omitted_payload = await _call_tool_over_fresh_http(
        window_uuid=window_uuid,
        tool_name="reply_message",
        arguments={
            "project_key": project_key,
            "message_id": original_id,
            "body_md": "Reply with sender_name omitted.",
            "idempotency_key": "http-window-omitted-reply-v1",
        },
    )
    assert omitted_status == 200
    assert not _jsonrpc_failed(omitted_payload)


@pytest.mark.asyncio
async def test_http_window_rebind_invalid_explicit_token_fails_closed(
    isolated_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    _config.clear_settings_cache()
    project_key = "/test/http-window-bad-explicit-token"
    window_uuid = str(uuid.uuid4())
    agent_name = await _create_window_bound_agent(
        monkeypatch,
        project_key=project_key,
        agent_name="BlueLake",
        window_uuid=window_uuid,
    )

    status_code, payload = await _call_tool_over_fresh_http(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": agent_name,
            "registration_token": "explicitly-invalid-token",
            "include_recent_commits": False,
        },
    )
    assert status_code == 200
    assert _jsonrpc_failed(payload)


@pytest.mark.asyncio
async def test_http_window_rebind_rejects_mismatched_and_unbound_identities(
    isolated_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    _config.clear_settings_cache()
    project_key = "/test/http-window-mismatch"
    window_uuid = str(uuid.uuid4())
    bound_name = await _create_window_bound_agent(
        monkeypatch,
        project_key=project_key,
        agent_name="BlueLake",
        window_uuid=window_uuid,
    )
    other_name, _ = await _create_registered_agent(
        project_key=project_key,
        agent_name="RedStone",
    )
    assert bound_name != other_name

    mismatch_status, mismatch_payload = await _call_tool_over_fresh_http(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": other_name,
            "include_recent_commits": False,
        },
    )
    unbound_status, unbound_payload = await _call_tool_over_fresh_http(
        window_uuid=str(uuid.uuid4()),
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": bound_name,
            "include_recent_commits": False,
        },
    )

    assert mismatch_status == 200
    assert _jsonrpc_failed(mismatch_payload)
    assert unbound_status == 200
    assert _jsonrpc_failed(unbound_payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_state", ["expired_window", "retired_agent"])
async def test_http_window_rebind_rejects_inactive_mapping_or_agent(
    isolated_env,
    monkeypatch,
    invalid_state: str,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    _config.clear_settings_cache()
    project_key = f"/test/http-window-{invalid_state}"
    window_uuid = str(uuid.uuid4())
    agent_name = await _create_window_bound_agent(
        monkeypatch,
        project_key=project_key,
        agent_name="BlueLake",
        window_uuid=window_uuid,
    )

    invalidated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    async with get_session() as session:
        if invalid_state == "expired_window":
            updated = await session.execute(
                text(
                    """
                    UPDATE window_identities
                    SET expires_ts = :invalidated_at
                    WHERE project_id = (
                        SELECT id FROM projects WHERE human_key = :project_key
                    ) AND window_uuid = :window_uuid
                    """
                ),
                {
                    "invalidated_at": invalidated_at,
                    "project_key": project_key,
                    "window_uuid": window_uuid,
                },
            )
        else:
            updated = await session.execute(
                text(
                    """
                    UPDATE agents
                    SET retired_at = :invalidated_at
                    WHERE project_id = (
                        SELECT id FROM projects WHERE human_key = :project_key
                    ) AND name = :agent_name
                    """
                ),
                {
                    "invalidated_at": invalidated_at,
                    "project_key": project_key,
                    "agent_name": agent_name,
                },
            )
        assert getattr(updated, "rowcount", 0) == 1
        await session.commit()

    status_code, payload = await _call_tool_over_fresh_http(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": agent_name,
            "include_recent_commits": False,
        },
    )
    assert status_code == 200
    assert _jsonrpc_failed(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_host", "extra_headers", "allow_localhost"),
    [
        pytest.param("203.0.113.10", None, True, id="remote-client"),
        pytest.param(
            "127.0.0.1",
            {"X-Forwarded-For": "203.0.113.10"},
            True,
            id="forwarded-loopback",
        ),
        pytest.param("127.0.0.1", None, False, id="localhost-bypass-disabled"),
    ],
)
async def test_http_window_rebind_requires_direct_unforwarded_loopback(
    isolated_env,
    monkeypatch,
    client_host: str,
    extra_headers: dict[str, str] | None,
    allow_localhost: bool,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    monkeypatch.setenv(
        "HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED",
        "true" if allow_localhost else "false",
    )
    _config.clear_settings_cache()
    project_key = "/test/http-window-transport-boundary"
    window_uuid = str(uuid.uuid4())
    agent_name = await _create_window_bound_agent(
        monkeypatch,
        project_key=project_key,
        agent_name="BlueLake",
        window_uuid=window_uuid,
    )

    status_code, payload = await _call_tool_over_fresh_http(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": agent_name,
            "include_recent_commits": False,
        },
        client_host=client_host,
        extra_headers=extra_headers,
    )
    _assert_http_or_tool_rejected(status_code, payload)


@pytest.mark.asyncio
async def test_http_core_window_uuid_bearer_never_authenticates(
    isolated_env,
    monkeypatch,
) -> None:
    project_key = "/test/http-core-window-is-routing-only"
    window_uuid = str(uuid.uuid4())
    encoded_pepper = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")
    monkeypatch.setenv("RUNTIME_PROFILE", "core")
    monkeypatch.setenv("CORE_OWNER_TOKEN", "test-core-owner")
    monkeypatch.setenv("CREDENTIAL_PEPPERS_JSON", json.dumps({"http-core": encoded_pepper}))
    monkeypatch.setenv("CREDENTIAL_CURRENT_PEPPER_KEY_ID", "http-core")
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    _config.clear_settings_cache()

    async with Client(build_mcp_server()) as client:
        await client.call_tool(
            "ensure_project",
            {"human_key": project_key, "owner_token": "test-core-owner"},
        )
        bootstrap = await client.call_tool(
            "issue_registration_bootstrap",
            {
                "project_key": project_key,
                "window_uuid": window_uuid,
                "owner_token": "test-core-owner",
            },
        )
        registered = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "codex",
                "model": "test-model",
                "name": "core-window-agent",
                "bootstrap_credential": bootstrap.data["bootstrap_credential"],
                "idempotency_key": "http-core-window-registration-v1",
            },
        )

    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    _config.clear_settings_cache()
    status_code, payload = await _call_tool_over_fresh_http(
        window_uuid=window_uuid,
        tool_name="whois",
        arguments={
            "project_key": project_key,
            "agent_name": registered.data["name"],
            "include_recent_commits": False,
        },
    )
    assert status_code == 200
    assert _jsonrpc_failed(payload)


@pytest.mark.asyncio
async def test_http_invalid_request_window_identity_blocks_process_env_fallback(isolated_env, monkeypatch):
    project_key = "/test/http-window-invalid"
    agent_name = "SilverRiver"
    window_uuid = str(uuid.uuid4())

    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "codex",
                "model": "test-model",
                "task_description": "invalid request identity test",
            },
        )
        agent_name = str(result.data["name"])

    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        env_fallback = await client.post(
            settings.http.path,
            json=_tool("whois", {"project_key": project_key, "agent_name": agent_name}),
        )
        assert env_fallback.status_code == 200
        assert _jsonrpc_failed(env_fallback.json())

        invalid_header = await client.post(
            settings.http.path,
            headers={"X-MCP-Agent-Mail-Window-ID": "not-a-uuid"},
            json=_tool("whois", {"project_key": project_key, "agent_name": agent_name}),
        )
        assert invalid_header.status_code == 200
        assert _jsonrpc_failed(invalid_header.json())


@pytest.mark.asyncio
async def test_http_jwks_validation_and_resource_rate_limit(isolated_env, monkeypatch):
    # Configure JWT with JWKS and strict resource rate limit
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    monkeypatch.setenv("HTTP_JWT_ALGORITHMS", "RS256")
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
    monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader")
    monkeypatch.setenv("HTTP_RBAC_WRITER_ROLES", "writer")
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE", "1")
    monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "10")
    # Provide a JWKS URL (dummy) and monkeypatch HTTP call
    monkeypatch.setenv("HTTP_JWT_JWKS_URL", "https://jwks.local/keys")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()

    # Generate RSA key + JWKS using Authlib utilities
    private_jwk = JsonWebKey.generate_key("RSA", 2048, is_private=True).as_dict(is_private=True)
    private_jwk["kid"] = "abc"
    public_jwk = JsonWebKey.import_key(private_jwk).as_dict(is_private=False)
    jwks_payload = {"keys": [public_jwk]}

    async def fake_get(self, url: str):
        class _Resp:
            status_code = 200
            def json(self) -> dict[str, Any]:
                return jwks_payload
        return _Resp()

    # Build token with RS256
    token = (
        jwt.encode(
            {"alg": "RS256", "kid": "abc"},
            {"sub": "u1", settings.http.jwt_role_claim: "reader"},
            private_jwk,
        ).decode("utf-8")
    )

    server = build_mcp_server()
    app = build_http_app(settings, server)

    # Patch httpx.AsyncClient.get used in JWKS fetch path
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        # Reader can call read-only tool
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 200
        # Resource rate limit 1 rpm -> second call 429
        r1 = await client.post(settings.http.path, headers=headers, json=_rpc("resources/read", {"uri": "resource://tooling/projects"}))
        assert r1.status_code in (200, 429)
        r2 = await client.post(settings.http.path, headers=headers, json=_rpc("resources/read", {"uri": "resource://tooling/projects"}))
        assert r2.status_code == 429


@pytest.mark.asyncio
async def test_http_path_mount_trailing_and_no_slash(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        base = settings.http.path.rstrip("/")
        r1 = await client.post(base, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r1.status_code in (200, 401, 403)
        r2 = await client.post(base + "/", json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r2.status_code in (200, 401, 403)


@pytest.mark.asyncio
async def test_http_readiness_endpoint(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/readiness")
        assert r.status_code in (200, 503)


@pytest.mark.asyncio
async def test_http_lock_status_endpoint(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)

    storage_root = Path(settings.storage.root).expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / ".archive.lock"
    lock_path.touch()
    metadata_path = storage_root / ".archive.lock.owner.json"
    metadata_path.write_text(json.dumps({"pid": 999_999, "created_ts": time.time() - 400}), encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/api/locks")
        assert resp.status_code == 200
        payload = resp.json()
        locks = payload.get("locks", [])
        assert any(item.get("path") == str(lock_path) for item in locks)
        entry = next(item for item in locks if item.get("path") == str(lock_path))
        assert entry.get("metadata", {}).get("pid") == 999_999
        assert entry.get("stale_suspected") is True
