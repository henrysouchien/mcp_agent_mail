from __future__ import annotations

import base64
import json

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import ConfigError, clear_settings_cache, get_settings


def _configure_credentials(monkeypatch) -> None:
    encoded = base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode("ascii")
    monkeypatch.setenv("CREDENTIAL_PEPPERS_JSON", json.dumps({"test-key": encoded}))
    monkeypatch.setenv("CREDENTIAL_CURRENT_PEPPER_KEY_ID", "test-key")
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "4f9e25cc-9150-4aa2-9188-300c7569a3f3")
    clear_settings_cache()


def test_credential_pepper_config_is_fail_closed(isolated_env: object, monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIAL_PEPPERS_JSON", '{"short":"eA"}')
    monkeypatch.setenv("CREDENTIAL_CURRENT_PEPPER_KEY_ID", "short")
    clear_settings_cache()
    with pytest.raises(ConfigError, match="at least 32 bytes"):
        get_settings()


@pytest.mark.asyncio
async def test_registration_issues_persistent_pane_bearer_and_uuid_is_not_authority(
    isolated_env: object,
    monkeypatch,
) -> None:
    _configure_credentials(monkeypatch)
    server = build_mcp_server()
    async with Client(server) as registration_client:
        await registration_client.call_tool(
            "ensure_project",
            {"human_key": "/pane-auth-integration"},
        )
        sender = await registration_client.call_tool(
            "register_agent",
            {
                "project_key": "/pane-auth-integration",
                "program": "test",
                "model": "test",
                "name": "BlueLake",
            },
        )
        recipient = await registration_client.call_tool(
            "register_agent",
            {
                "project_key": "/pane-auth-integration",
                "program": "test",
                "model": "test",
                "name": "RedStone",
            },
        )
        assert "pane_credential" in sender.data
        await registration_client.call_tool(
            "set_contact_policy",
            {
                "project_key": "/pane-auth-integration",
                "agent_name": recipient.data["name"],
                "policy": "open",
            },
        )

    arguments = {
        "project_key": "/pane-auth-integration",
        "sender_name": sender.data["name"],
        "to": [recipient.data["name"]],
        "subject": "Persistent pane authentication",
        "body_md": "The window UUID alone is not the bearer.",
    }
    async with Client(server) as fresh_client:
        with pytest.raises(Exception, match="sender_token"):
            await fresh_client.call_tool("send_message", arguments)
        authenticated_arguments = dict(arguments)
        authenticated_arguments["sender_token"] = sender.data["pane_credential"]
        result = await fresh_client.call_tool("send_message", authenticated_arguments)
    assert result.data["count"] == 1
