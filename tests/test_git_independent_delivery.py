from __future__ import annotations

import base64
from pathlib import Path

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
    Blob,
    BlobReference,
    IdempotencyRecord,
    Message,
    Project,
    ProjectStorageCutover,
)


async def _create_git_independent_project(human_key: str, slug: str) -> int:
    async with get_immediate_session() as session:
        project = Project(slug=slug, human_key=human_key)
        session.add(project)
        await session.flush()
        assert project.id is not None
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
        return project.id


@pytest.mark.asyncio
async def test_core_ensure_project_bootstraps_baseline_without_archive(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "core")
    clear_settings_cache()

    async def archive_must_not_open(*args, **kwargs):
        raise AssertionError("Core project bootstrap attempted to open the legacy archive")

    monkeypatch.setattr("mcp_agent_mail.app.ensure_archive", archive_must_not_open)
    async with Client(build_mcp_server()) as client:
        first = await client.call_tool(
            "ensure_project",
            {"human_key": "/new-core-project"},
        )
        second = await client.call_tool(
            "ensure_project",
            {"human_key": "/new-core-project"},
        )
        registered = await client.call_tool(
            "register_agent",
            {
                "project_key": "/new-core-project",
                "program": "test",
                "model": "test",
                "name": "core-1",
            },
        )

    assert second.data["id"] == first.data["id"]
    assert registered.data["name"] == "core-1"
    async with get_session() as session:
        cutover = await session.get(ProjectStorageCutover, first.data["id"])
        assert cutover is not None
        assert cutover.state == "git_independent"
        assert cutover.baseline_event_id is not None
        assert await session.scalar(select(func.count()).select_from(Project)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 2


@pytest.mark.asyncio
async def test_live_registration_never_opens_archive_for_git_independent_project(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "migration")
    clear_settings_cache()
    await ensure_schema()
    project_id = await _create_git_independent_project(
        "/git-independent-registration",
        "git-independent-registration",
    )

    async def archive_must_not_open(*args, **kwargs):
        raise AssertionError("Git-independent registration attempted to open the legacy archive")

    monkeypatch.setattr("mcp_agent_mail.app.ensure_archive", archive_must_not_open)
    async with Client(build_mcp_server()) as client:
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/git-independent-registration",
                "program": "test",
                "model": "test",
                "name": "BlueLake",
            },
        )
    assert result.data["name"] == "BlueLake"
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 2
        chain = await verify_audit_chain(session, project_id)
    assert chain.valid is True


@pytest.mark.asyncio
async def test_live_send_installs_blob_attachments_before_atomic_references(
    isolated_env: object,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "migration")
    monkeypatch.setenv("ALLOW_ABSOLUTE_ATTACHMENT_PATHS", "true")
    monkeypatch.setenv("BLOB_STORAGE_ROOT", str(tmp_path / "blobs"))
    clear_settings_cache()
    await ensure_schema()
    async with get_immediate_session() as session:
        project = Project(slug="blob-live", human_key=str(tmp_path))
        session.add(project)
        await session.flush()
        assert project.id is not None
        sender = Agent(
            project_id=project.id,
            name="BlobSender",
            program="test",
            model="test",
            registration_token="sender-secret",
            contact_policy="open",
        )
        recipient = Agent(
            project_id=project.id,
            name="BlobRecipient",
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

    file_bytes = b"durable attachment bytes"
    attachment_path = tmp_path / "evidence.txt"
    attachment_path.write_bytes(file_bytes)
    inline_bytes = b"inline image bytes"
    inline_uri = base64.b64encode(inline_bytes).decode("ascii")
    arguments = {
        "project_key": str(tmp_path),
        "sender_name": "BlobSender",
        "sender_token": "sender-secret",
        "to": ["BlobRecipient"],
        "subject": "Blob-backed",
        "body_md": f"proof ![image](data:image/png;base64,{inline_uri})",
        "attachment_paths": [str(attachment_path)],
        "idempotency_key": "blob-live-key",
    }
    async with Client(build_mcp_server()) as client:
        first = await client.call_tool("send_message", arguments)
        replay = await client.call_tool("send_message", arguments)

    first_payload = first.data["deliveries"][0]["payload"]
    replay_payload = replay.data["deliveries"][0]["payload"]
    assert first_payload["replayed"] is False
    assert replay_payload["replayed"] is True
    assert replay_payload["id"] == first_payload["id"]
    assert len(first_payload["attachments"]) == 2
    assert "data:image" not in first_payload["body_md"]
    assert first_payload["body_md"].count("blob:sha256:") == 1

    async with get_session() as session:
        message = (await session.execute(select(Message))).scalar_one()
        blobs = (await session.execute(select(Blob))).scalars().all()
        references = (await session.execute(select(BlobReference))).scalars().all()
    assert "data:image" not in message.body_md
    assert len(blobs) == 2
    assert len(references) == 2
    for blob in blobs:
        object_path = tmp_path / "blobs" / blob.storage_key
        assert object_path.is_file()
        assert object_path.read_bytes() in {file_bytes, inline_bytes}
    assert list((tmp_path / "blobs" / "leases" / "install").glob("*.lease")) == []


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
