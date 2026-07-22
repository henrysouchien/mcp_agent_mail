from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.audit import append_audit_event, verify_audit_chain
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.models import (
    Agent,
    AuditEvent,
    Blob,
    BlobReference,
    BootstrapCredential,
    IdempotencyRecord,
    Message,
    MessageRecipient,
    PaneCredential,
    Project,
    ProjectStorageCutover,
)


def _configure_managed_registration(monkeypatch) -> None:
    encoded = base64.urlsafe_b64encode(b"m" * 32).rstrip(b"=").decode("ascii")
    monkeypatch.setenv("CREDENTIAL_PEPPERS_JSON", json.dumps({"managed-test": encoded}))
    monkeypatch.setenv("CREDENTIAL_CURRENT_PEPPER_KEY_ID", "managed-test")
    monkeypatch.setenv("CORE_OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv(
        "MCP_AGENT_MAIL_WINDOW_ID",
        "95ae694c-0210-4baa-88ab-62dc69295dc9",
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
async def test_core_overseer_send_rejects_before_any_mutation(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "core")
    _configure_managed_registration(monkeypatch)
    clear_settings_cache()
    await ensure_schema()
    project_id = await _create_git_independent_project(
        "/core-overseer-rejection",
        "core-overseer-rejection",
    )
    async with get_session() as session:
        session.add(
            Agent(
                project_id=project_id,
                name="BlueLake",
                program="test",
                model="test",
                task_description="recipient",
                registration_token="recipient-token",
            )
        )
        await session.commit()

    settings = get_settings()
    app = build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mail/core-overseer-rejection/overseer/send",
            json={
                "recipients": ["BlueLake"],
                "subject": "Must not commit",
                "body_md": "This legacy path must fail before mutation.",
            },
        )

    assert response.status_code == 409
    async with get_session() as session:
        message_count = await session.scalar(
            select(func.count()).select_from(Message).where(Message.project_id == project_id)
        )
        overseer_count = await session.scalar(
            select(func.count()).select_from(Agent).where(
                Agent.project_id == project_id,
                Agent.name == "HumanOverseer",
            )
        )
    assert message_count == 0
    assert overseer_count == 0


@pytest.mark.asyncio
async def test_core_ensure_project_bootstraps_baseline_without_archive(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "core")
    _configure_managed_registration(monkeypatch)
    clear_settings_cache()

    async def archive_must_not_open(*args, **kwargs):
        raise AssertionError("Core project bootstrap attempted to open the legacy archive")

    monkeypatch.setattr("mcp_agent_mail.app.ensure_archive", archive_must_not_open)
    async with Client(build_mcp_server()) as client:
        first = await client.call_tool(
            "ensure_project",
            {"human_key": "/new-core-project", "owner_token": "owner-secret"},
        )
        second = await client.call_tool(
            "ensure_project",
            {"human_key": "/new-core-project", "owner_token": "owner-secret"},
        )
        bootstrap = await client.call_tool(
            "issue_registration_bootstrap",
            {
                "project_key": "/new-core-project",
                "window_uuid": "95ae694c-0210-4baa-88ab-62dc69295dc9",
                "owner_token": "owner-secret",
            },
        )
        registration_arguments = {
            "project_key": "/new-core-project",
            "program": "test",
            "model": "test",
            "name": "core-1",
            "bootstrap_credential": bootstrap.data["bootstrap_credential"],
            "idempotency_key": "register-core-1",
        }
        registered = await client.call_tool(
            "register_agent",
            registration_arguments,
        )
        async with get_immediate_session() as session:
            consumed_bootstrap = (
                await session.execute(select(BootstrapCredential))
            ).scalar_one()
            consumed_bootstrap.expires_ts = (
                datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            )
            await session.commit()
        replay = await client.call_tool(
            "register_agent",
            registration_arguments,
        )

    assert second.data["id"] == first.data["id"]
    assert registered.data["name"] == "core-1"
    assert registered.data["registration_token"] == replay.data["registration_token"]
    assert registered.data["pane_credential"] == replay.data["pane_credential"]
    assert replay.data["replayed"] is True
    async with get_session() as session:
        cutover = await session.get(ProjectStorageCutover, first.data["id"])
        assert cutover is not None
        assert cutover.state == "git_independent"
        assert cutover.baseline_event_id is not None
        assert await session.scalar(select(func.count()).select_from(Project)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 3
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


@pytest.mark.asyncio
async def test_owner_can_rotate_revoke_and_reissue_pane_credentials(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "core")
    _configure_managed_registration(monkeypatch)
    clear_settings_cache()
    window_uuid = "95ae694c-0210-4baa-88ab-62dc69295dc9"

    async with Client(build_mcp_server()) as client:
        await client.call_tool(
            "ensure_project",
            {"human_key": "/credential-lifecycle", "owner_token": "owner-secret"},
        )
        bootstrap = await client.call_tool(
            "issue_registration_bootstrap",
            {
                "project_key": "/credential-lifecycle",
                "window_uuid": window_uuid,
                "owner_token": "owner-secret",
            },
        )
        registered = await client.call_tool(
            "register_agent",
            {
                "project_key": "/credential-lifecycle",
                "program": "test",
                "model": "test",
                "name": "credential-1",
                "bootstrap_credential": bootstrap.data["bootstrap_credential"],
                "idempotency_key": "register-credential-1",
            },
        )
        credential_id = str(registered.data["pane_credential"]).split(".", 1)[0]
        rotated = await client.call_tool(
            "rotate_pane_credential",
            {
                "project_key": "/credential-lifecycle",
                "credential_id": credential_id,
                "expected_generation": 1,
                "owner_token": "owner-secret",
            },
        )
        assert rotated.data["generation"] == 2
        revoked = await client.call_tool(
            "revoke_pane_credential",
            {
                "project_key": "/credential-lifecycle",
                "credential_id": credential_id,
                "owner_token": "owner-secret",
                "reason": "test compromise",
            },
        )
        assert revoked.data["revoked"] is True
        reissued = await client.call_tool(
            "reissue_pane_credential",
            {
                "project_key": "/credential-lifecycle",
                "agent_name": "credential-1",
                "window_uuid": window_uuid,
                "owner_token": "owner-secret",
            },
        )
        retired = await client.call_tool(
            "deregister_agent",
            {
                "project_key": "/credential-lifecycle",
                "agent_name": "credential-1",
            },
        )
        with pytest.raises(Exception, match="retired"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/credential-lifecycle",
                    "sender_name": "credential-1",
                    "sender_token": registered.data["registration_token"],
                    "to": ["credential-1"],
                    "subject": "must fail",
                    "body_md": "deregistered identities cannot send",
                },
            )
        with pytest.raises(Exception, match="owner"):
            await client.call_tool(
                "unretire_agent",
                {
                    "project_key": "/credential-lifecycle",
                    "agent_name": "credential-1",
                    "registration_token": registered.data["registration_token"],
                },
            )
        with pytest.raises(Exception, match="retired"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/credential-lifecycle",
                    "to": ["credential-1"],
                    "subject": "bound session must fail",
                    "body_md": "permanent binding cannot bypass retirement",
                },
            )
        with pytest.raises(Exception, match="retired"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/credential-lifecycle",
                    "sender_name": "credential-1",
                    "sender_token": reissued.data["pane_credential"],
                    "to": ["credential-1"],
                    "subject": "must also fail",
                    "body_md": "revoked pane identities cannot send",
                },
            )
        restored = await client.call_tool(
            "unretire_agent",
            {
                "project_key": "/credential-lifecycle",
                "agent_name": "credential-1",
                "owner_token": "owner-secret",
            },
        )
        with pytest.raises(Exception, match="Invalid pane credential"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/credential-lifecycle",
                    "sender_token": reissued.data["pane_credential"],
                    "to": ["credential-1"],
                    "subject": "revoked pane remains invalid",
                    "body_md": "owner unretire does not revive pane credentials",
                },
            )
        resumed = await client.call_tool(
            "send_message",
            {
                "project_key": "/credential-lifecycle",
                "sender_name": "credential-1",
                "sender_token": registered.data["registration_token"],
                "to": ["credential-1"],
                "subject": "registration authority restored",
                "body_md": "owner unretire restores active identity state",
                "idempotency_key": "post-unretire-send",
            },
        )

    assert reissued.data["credential_id"] == credential_id
    assert reissued.data["generation"] == 3
    assert retired.data["revoked_pane_credentials"] == 1
    assert restored.data["active_pane_credentials"] == 0
    assert resumed.data["count"] == 1
    async with get_session() as session:
        active_count = await session.scalar(
            select(func.count()).select_from(PaneCredential).where(
                cast(Any, PaneCredential.revoked_ts).is_(None)
            )
        )
        assert active_count == 0


@pytest.mark.asyncio
async def test_core_startup_rejects_existing_legacy_project(
    isolated_env: object,
    monkeypatch,
) -> None:
    await ensure_schema()
    async with get_immediate_session() as session:
        session.add(Project(slug="legacy-at-core-start", human_key="/legacy-at-core-start"))
        await session.commit()
    monkeypatch.setenv("RUNTIME_PROFILE", "core")
    _configure_managed_registration(monkeypatch)
    clear_settings_cache()

    with pytest.raises(Exception, match="core startup refused non-core projects"):
        async with Client(build_mcp_server()):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_profile", ["migration", "database"])
async def test_managed_registration_follows_project_route_outside_core_profile(
    isolated_env: object,
    monkeypatch,
    runtime_profile: str,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", runtime_profile)
    _configure_managed_registration(monkeypatch)
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
        with pytest.raises(Exception, match="Managed registration requires"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": "/git-independent-registration",
                    "program": "test",
                    "model": "test",
                    "name": "BlueLake",
                },
            )
        bootstrap = await client.call_tool(
            "issue_registration_bootstrap",
            {
                "project_key": "/git-independent-registration",
                "window_uuid": "95ae694c-0210-4baa-88ab-62dc69295dc9",
                "owner_token": "owner-secret",
            },
        )
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": "/git-independent-registration",
                "program": "test",
                "model": "test",
                "name": "BlueLake",
                "bootstrap_credential": bootstrap.data["bootstrap_credential"],
                "idempotency_key": "register-blue-lake",
            },
        )
    assert result.data["name"] == "BlueLake"
    assert result.data["pane_credential"]
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 1
        consumed_bootstrap = (
            await session.execute(select(BootstrapCredential))
        ).scalar_one()
        assert consumed_bootstrap.consumed_ts is not None
        assert await session.scalar(select(func.count()).select_from(PaneCredential)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 3
        chain = await verify_audit_chain(session, project_id)
    assert chain.valid is True


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_profile", ["migration", "database"])
async def test_managed_unretire_follows_project_route_outside_core_profile(
    isolated_env: object,
    monkeypatch,
    runtime_profile: str,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", runtime_profile)
    _configure_managed_registration(monkeypatch)
    clear_settings_cache()
    await ensure_schema()
    project_id = await _create_git_independent_project(
        "/git-independent-unretire",
        "git-independent-unretire",
    )
    async with get_immediate_session() as session:
        agent = Agent(
            project_id=project_id,
            name="BlueLake",
            program="test",
            model="test",
            registration_token="self-secret",
            retired_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.add(agent)
        await session.commit()

    async with Client(build_mcp_server()) as client:
        with pytest.raises(Exception, match="owner"):
            await client.call_tool(
                "unretire_agent",
                {
                    "project_key": "/git-independent-unretire",
                    "agent_name": "BlueLake",
                    "registration_token": "self-secret",
                },
            )
        restored = await client.call_tool(
            "unretire_agent",
            {
                "project_key": "/git-independent-unretire",
                "agent_name": "BlueLake",
                "owner_token": "owner-secret",
            },
        )

    assert restored.data["status"] == "active"
    async with get_session() as session:
        db_agent = (
            await session.execute(
                select(Agent).where(cast(Any, Agent.name) == "BlueLake")
            )
        ).scalar_one()
        assert db_agent.retired_at is None


@pytest.mark.asyncio
async def test_database_profile_uses_existing_db_without_cutover_or_git(
    isolated_env: object,
    monkeypatch,
) -> None:
    """Ordinary projects get atomic/replay-safe writes without a cutover row."""
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    clear_settings_cache()

    async def legacy_path_must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("database profile crossed the legacy Git/archive boundary")

    monkeypatch.setattr("mcp_agent_mail.app.ensure_archive", legacy_path_must_not_run)
    monkeypatch.setattr("mcp_agent_mail.app.write_agent_profile", legacy_path_must_not_run)
    monkeypatch.setattr("mcp_agent_mail.app.write_message_bundle", legacy_path_must_not_run)

    async with Client(build_mcp_server()) as client:
        project = await client.call_tool(
            "ensure_project",
            {"human_key": "/database-authoritative"},
        )
        sender = await client.call_tool(
            "register_agent",
            {
                "project_key": "/database-authoritative",
                "program": "test",
                "model": "test",
                "name": "BlueLake",
            },
        )
        recipient = await client.call_tool(
            "register_agent",
            {
                "project_key": "/database-authoritative",
                "program": "test",
                "model": "test",
                "name": "GreenCastle",
            },
        )
        send_arguments = {
            "project_key": "/database-authoritative",
            "sender_name": "BlueLake",
            "sender_token": sender.data["registration_token"],
            "to": ["GreenCastle"],
            "subject": "Retry-safe without Git",
            "body_md": "The first response may be lost.",
        }
        first_send = await client.call_tool("send_message", send_arguments)
        replayed_send = await client.call_tool("send_message", send_arguments)
        message_id = first_send.data["deliveries"][0]["payload"]["id"]

        reply_arguments = {
            "project_key": "/database-authoritative",
            "message_id": message_id,
            "sender_name": "GreenCastle",
            "sender_token": recipient.data["registration_token"],
            "body_md": "Acknowledged.",
        }
        first_reply = await client.call_tool("reply_message", reply_arguments)
        replayed_reply = await client.call_tool("reply_message", reply_arguments)

        reserve_arguments = {
            "project_key": "/database-authoritative",
            "agent_name": "BlueLake",
            "registration_token": sender.data["registration_token"],
            "paths": ["src/database.py"],
            "ttl_seconds": 600,
        }
        first_reserve = await client.call_tool("file_reservation_paths", reserve_arguments)
        replayed_reserve = await client.call_tool("file_reservation_paths", reserve_arguments)
        release_arguments = {
            "project_key": "/database-authoritative",
            "agent_name": "BlueLake",
            "registration_token": sender.data["registration_token"],
            "paths": ["src/database.py"],
        }
        first_release = await client.call_tool(
            "release_file_reservations",
            release_arguments,
        )
        replayed_release = await client.call_tool(
            "release_file_reservations",
            release_arguments,
        )

    first_payload = first_send.data["deliveries"][0]["payload"]
    replayed_payload = replayed_send.data["deliveries"][0]["payload"]
    assert first_payload["replayed"] is False
    assert replayed_payload["replayed"] is True
    assert replayed_payload["id"] == first_payload["id"]
    assert replayed_payload["retry_safety"] == "safe_with_automatic_retry_dedupe"
    assert first_reply.data["replayed"] is False
    assert replayed_reply.data["replayed"] is True
    assert replayed_reply.data["id"] == first_reply.data["id"]
    assert first_reserve.data["replayed"] is False
    assert replayed_reserve.data["replayed"] is False
    assert replayed_reserve.data["granted"][0]["id"] == first_reserve.data["granted"][0]["id"]
    assert replayed_reserve.data["granted"][0]["reused"] is True
    assert replayed_reserve.data["idempotency_mode"] == "state"
    assert first_release.data["released"] == 1
    assert replayed_release.data["released"] == 0
    assert replayed_release.data["replayed"] is False
    assert replayed_release.data["idempotency_mode"] == "state"

    async with get_session() as session:
        assert await session.get(ProjectStorageCutover, project.data["id"]) is None
        assert await session.scalar(select(func.count()).select_from(Project)) == 1
        assert await session.scalar(select(func.count()).select_from(Agent)) == 2
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 2


@pytest.mark.asyncio
async def test_managed_registration_collapses_one_hundred_public_tool_contenders(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "core")
    _configure_managed_registration(monkeypatch)
    clear_settings_cache()
    async with Client(build_mcp_server()) as client:
        await client.call_tool(
            "ensure_project",
            {"human_key": "/registration-race", "owner_token": "owner-secret"},
        )
        bootstrap = await client.call_tool(
            "issue_registration_bootstrap",
            {
                "project_key": "/registration-race",
                "window_uuid": "95ae694c-0210-4baa-88ab-62dc69295dc9",
                "owner_token": "owner-secret",
            },
        )
        arguments = {
            "project_key": "/registration-race",
            "program": "test",
            "model": "test",
            "name": "race-1",
            "bootstrap_credential": bootstrap.data["bootstrap_credential"],
            "idempotency_key": "registration-race-key",
        }
        results = await asyncio.gather(
            *(client.call_tool("register_agent", arguments) for _ in range(100))
        )

    assert {result.data["id"] for result in results} == {results[0].data["id"]}
    assert {result.data["pane_credential"] for result in results} == {
        results[0].data["pane_credential"]
    }
    assert sum(result.data["replayed"] is False for result in results) == 1
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 1
        assert await session.scalar(select(func.count()).select_from(PaneCredential)) == 1
        assert await session.scalar(select(func.count()).select_from(BootstrapCredential)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


@pytest.mark.asyncio
async def test_managed_registration_rolls_back_before_commit_fault(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "core")
    _configure_managed_registration(monkeypatch)
    clear_settings_cache()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool(
            "ensure_project",
            {"human_key": "/registration-fault", "owner_token": "owner-secret"},
        )
        bootstrap = await client.call_tool(
            "issue_registration_bootstrap",
            {
                "project_key": "/registration-fault",
                "window_uuid": "95ae694c-0210-4baa-88ab-62dc69295dc9",
                "owner_token": "owner-secret",
            },
        )

        async def fail_before_secret_derivation(*args, **kwargs):
            raise RuntimeError("injected pre-commit failure")

        monkeypatch.setattr(
            "mcp_agent_mail.app.derive_registration_credentials",
            fail_before_secret_derivation,
        )
        with pytest.raises(Exception, match="injected pre-commit failure"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": "/registration-fault",
                    "program": "test",
                    "model": "test",
                    "name": "fault-1",
                    "bootstrap_credential": bootstrap.data["bootstrap_credential"],
                    "idempotency_key": "registration-fault-key",
                },
            )

    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 0
        assert await session.scalar(select(func.count()).select_from(PaneCredential)) == 0
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0
        bootstrap_row = (
            await session.execute(select(BootstrapCredential))
        ).scalar_one()
        assert bootstrap_row.consumed_ts is None


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
    assert "body_md" not in first_payload

    async with get_session() as session:
        message = (await session.execute(select(Message))).scalar_one()
        blobs = (await session.execute(select(Blob))).scalars().all()
        references = (await session.execute(select(BlobReference))).scalars().all()
    assert "data:image" not in message.body_md
    assert message.body_md.count("blob:sha256:") == 1
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
        first_message_id = int(first.data["deliveries"][0]["payload"]["id"])
        reply_arguments = {
            "project_key": "/git-independent-live",
            "message_id": first_message_id,
            "sender_name": "AtomicRecipient",
            "sender_token": "recipient-secret",
            "to": ["AtomicSender"],
            "body_md": "Reply committed before visibility changes",
            "idempotency_key": "reply-live-route-key",
        }
        first_reply = await client.call_tool("reply_message", reply_arguments)
        marked = await client.call_tool(
            "mark_message_read",
            {
                "project_key": "/git-independent-live",
                "agent_name": "AtomicRecipient",
                "registration_token": "recipient-secret",
                "message_id": first_message_id,
            },
        )
        acknowledged = await client.call_tool(
            "acknowledge_message",
            {
                "project_key": "/git-independent-live",
                "agent_name": "AtomicRecipient",
                "registration_token": "recipient-secret",
                "message_id": first_message_id,
            },
        )
        assert marked.data["audit_event_id"] is not None
        assert acknowledged.data["audit_event_id"] is not None
        async with get_immediate_session() as session:
            recipient = (
                await session.execute(
                    select(Agent).where(cast(Any, Agent.name) == "AtomicRecipient")
                )
            ).scalar_one()
            original_delivery = await session.get(
                MessageRecipient,
                (first_message_id, recipient.id),
            )
            assert original_delivery is not None
            await session.delete(original_delivery)
            await session.commit()
        reply_replay = await client.call_tool("reply_message", reply_arguments)
        async with get_immediate_session() as session:
            recipient = (
                await session.execute(
                    select(Agent).where(cast(Any, Agent.name) == "AtomicRecipient")
                )
            ).scalar_one()
            recipient.retired_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
        replay = await client.call_tool("send_message", arguments)

    first_payload = first.data["deliveries"][0]["payload"]
    replay_payload = replay.data["deliveries"][0]["payload"]
    assert first_payload["replayed"] is False
    assert replay_payload["replayed"] is True
    assert replay_payload["id"] == first_payload["id"]
    assert replay_payload["retry_safety"] == "safe_with_idempotency_key"
    assert reply_replay.data["replayed"] is True
    assert reply_replay.data["id"] == first_reply.data["id"]
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 2
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 5
        chain = await verify_audit_chain(session, project_id)
    assert chain.valid is True
