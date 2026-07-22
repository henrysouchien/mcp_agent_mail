from __future__ import annotations

import pytest
from fastmcp import Client
from sqlalchemy import func, select

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.audit import append_audit_event, verify_audit_chain
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session
from mcp_agent_mail.models import (
    Agent,
    AuditEvent,
    IdempotencyRecord,
    Message,
    Project,
    ProjectStorageCutover,
)


@pytest.mark.asyncio
async def test_live_send_uses_atomic_path_and_never_opens_archive(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "migration")
    clear_settings_cache()
    await ensure_schema()
    async with get_immediate_session() as session:
        project = Project(slug="git-independent-live", human_key="/git-independent-live")
        session.add(project)
        await session.flush()
        assert project.id is not None
        sender = Agent(
            project_id=project.id,
            name="AtomicSender",
            program="test",
            model="test",
            registration_token="sender-secret",
            contact_policy="open",
        )
        recipient = Agent(
            project_id=project.id,
            name="AtomicRecipient",
            program="test",
            model="test",
            registration_token="recipient-secret",
            contact_policy="open",
        )
        session.add_all([sender, recipient])
        await session.flush()
        baseline = await append_audit_event(
            session,
            project_id=project.id,
            actor_kind="system",
            actor_scope_id="system/new-project",
            actor_agent_id=None,
            operation_kind="project_created_v1",
            entity_type="project",
            entity_id=str(project.id),
            payload_version="project-created-v1",
            payload={"slug": project.slug},
        )
        await session.flush()
        assert baseline.id is not None
        session.add(
            ProjectStorageCutover(
                project_id=project.id,
                state="git_independent",
                generation=1,
                baseline_event_id=baseline.id,
            )
        )
        await session.commit()
        project_id = project.id

    async def archive_must_not_open(*args, **kwargs):
        raise AssertionError("Git-independent delivery attempted to open the legacy archive")

    monkeypatch.setattr("mcp_agent_mail.app.ensure_archive", archive_must_not_open)
    server = build_mcp_server()
    arguments = {
        "project_key": "/git-independent-live",
        "sender_name": "AtomicSender",
        "sender_token": "sender-secret",
        "to": ["AtomicRecipient"],
        "subject": "Atomic live route",
        "body_md": "No Git archive involved",
        "idempotency_key": "live-route-key",
    }
    async with Client(server) as client:
        first = await client.call_tool("send_message", arguments)
        replay = await client.call_tool("send_message", arguments)

    first_payload = first.data["deliveries"][0]["payload"]
    replay_payload = replay.data["deliveries"][0]["payload"]
    assert first_payload["replayed"] is False
    assert replay_payload["replayed"] is True
    assert replay_payload["id"] == first_payload["id"]
    assert replay_payload["retry_safety"] == "safe_with_idempotency_key"
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 2
        chain = await verify_audit_chain(session, project_id)
    assert chain.valid is True
