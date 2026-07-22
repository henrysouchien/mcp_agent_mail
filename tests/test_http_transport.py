from __future__ import annotations

import contextlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from authlib.jose import JsonWebKey, jwt
from fastmcp import Client
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
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
    return str(result.data["name"])


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
async def test_http_window_identity_bearer_prefix_is_not_authentication(isolated_env, monkeypatch):
    project_key = "/test/http-window-bearer"
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
        response = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool("whois", {"project_key": project_key, "agent_name": agent_name}),
        )
        assert response.status_code == 200
        assert _jsonrpc_failed(response.json())


@pytest.mark.asyncio
async def test_http_token_backed_whois_does_not_turn_window_id_into_bearer(isolated_env, monkeypatch):
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
            json=_tool("whois", {"project_key": project_key, "agent_name": agent_name}),
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
            json=_tool("whois", {"project_key": project_key, "agent_name": agent_name}),
        )
        assert after.status_code == 200
        assert _jsonrpc_failed(after.json())


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
            json=_tool("whois", {"project_key": project_key, "agent_name": first_agent}),
        )
        assert first_still_bound.status_code == 200
        assert _jsonrpc_failed(first_still_bound.json())

        second_not_bound = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer mcp-window:{window_uuid}"},
            json=_tool("whois", {"project_key": project_key, "agent_name": second_agent}),
        )
        assert second_not_bound.status_code == 200
        assert _jsonrpc_failed(second_not_bound.json())


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
