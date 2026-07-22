from __future__ import annotations

import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.rich_logger import ToolCallContext, _safe_json_format, render_tool_call_panel


def test_rich_tool_logging_redacts_nested_credentials() -> None:
    secrets = {
        "registration_token": "registration-secret",
        "nested": {
            "sender-token": "sender-secret",
            "Authorization": "Bearer transport-secret",
            "safe": "visible-value",
        },
        "requester_registration_token": "requester-secret",
    }
    ctx = ToolCallContext(
        tool_name="send_message",
        args=[],
        kwargs=secrets,
        result={"registration_token": "result-secret", "status": "ok"},
    )
    ctx.end_time = ctx.start_time + 0.01

    rendered = render_tool_call_panel(ctx)
    serialized_params = _safe_json_format(secrets)

    for secret in (
        "registration-secret",
        "sender-secret",
        "transport-secret",
        "requester-secret",
        "result-secret",
    ):
        assert secret not in rendered
        assert secret not in serialized_params
    assert rendered.count("[REDACTED]") >= 1
    assert serialized_params.count("[REDACTED]") >= 4
    assert "visible-value" in serialized_params
    assert '"status": "ok"' in rendered


@pytest.mark.asyncio
async def test_log_json_enabled_path(isolated_env, monkeypatch):
    # Enable JSON logging in settings to hit JSONRenderer branch
    monkeypatch.setenv("LOG_JSON_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Liveness should work; logging config path executed on app build
        r = await client.get("/health/liveness")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_redis_fallback(isolated_env, monkeypatch):
    # Force redis backend but make import fail so it falls back to memory
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HTTP_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("HTTP_RATE_LIMIT_REDIS_URL", "redis://localhost:6379/0")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    # Simulate import failure by shadowing importlib.import_module to raise for redis.asyncio
    import importlib

    def fake_import(name: str, *a, **k):
        if name == "redis.asyncio":
            raise ImportError("no redis")
        return real_import(name, *a, **k)

    real_import = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", fake_import)

    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/liveness")
        assert r.status_code == 200
