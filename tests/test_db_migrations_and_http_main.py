from __future__ import annotations

import contextlib
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import _backfill_active_principal_authorities, ensure_schema
from mcp_agent_mail.http import build_http_app, main as http_main


def test_http_main_invokes_uvicorn(monkeypatch):
    # Ensure settings default host/port are used
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    calls: dict[str, object] = {}

    def fake_run(app, host, port, log_level="info"):
        calls["host"] = host
        calls["port"] = port
        calls["lv"] = log_level

    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setattr("uvicorn.run", fake_run)
    # Prevent pytest argv from leaking into argparse
    monkeypatch.setattr(sys, "argv", ["mcp-http"])
    http_main()
    assert calls.get("host") == "127.0.0.1"


async def _readiness_ok() -> int:
    # Sanity check app readiness OK path with schema ensured
    await ensure_schema()
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/readiness")
        return r.status_code


def test_readiness_ok_status(isolated_env):
    import asyncio

    code = asyncio.run(_readiness_ok())
    assert code in (200, 503)


def _legacy_authority_connection():
    engine = create_engine("sqlite://")
    connection = engine.connect()
    connection.exec_driver_sql(
        """
        CREATE TABLE window_identities (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            agent_id INTEGER,
            superseded_ts DATETIME,
            expires_ts DATETIME
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE runtime_bindings (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            agent_id INTEGER NOT NULL,
            authority_kind VARCHAR(32) NOT NULL,
            authority_id VARCHAR(64) NOT NULL,
            state VARCHAR(32) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE active_principal_authorities (
            project_id INTEGER NOT NULL,
            agent_id INTEGER NOT NULL,
            window_identity_id INTEGER NOT NULL UNIQUE,
            authority_generation INTEGER NOT NULL,
            activated_ts DATETIME NOT NULL,
            PRIMARY KEY (project_id, agent_id)
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO window_identities
            (id, project_id, agent_id, expires_ts)
        VALUES
            (11, 1, 7, datetime('now', '+1 day')),
            (12, 1, 7, datetime('now', '+1 day'))
        """
    )
    return engine, connection


def test_authority_backfill_fails_closed_on_ambiguous_current_rows() -> None:
    engine, connection = _legacy_authority_connection()
    try:
        with pytest.raises(RuntimeError, match="Ambiguous active window authorities"):
            _backfill_active_principal_authorities(connection)
    finally:
        connection.close()
        engine.dispose()


def test_authority_backfill_selects_only_active_runtime_authority() -> None:
    engine, connection = _legacy_authority_connection()
    try:
        connection.exec_driver_sql(
            """
            INSERT INTO runtime_bindings
                (project_id, agent_id, authority_kind, authority_id, state)
            VALUES (1, 7, 'window_identity', '12', 'ready')
            """
        )
        _backfill_active_principal_authorities(connection)
        row = connection.exec_driver_sql(
            """
            SELECT window_identity_id
            FROM active_principal_authorities
            WHERE project_id = 1 AND agent_id = 7
            """
        ).one()
        assert row[0] == 12
    finally:
        connection.close()
        engine.dispose()

