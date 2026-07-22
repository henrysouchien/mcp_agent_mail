from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from mcp_agent_mail.audit import (
    AUDIT_GENESIS_HASH,
    AuditPayloadError,
    append_audit_event,
    canonical_json,
    verify_audit_chain,
)
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session
from mcp_agent_mail.models import Project


def test_canonical_json_normalizes_keys_strings_and_order() -> None:
    decomposed = "e\u0301"
    assert canonical_json({"z": [True, 3], "é": decomposed, "a": None}) == (
        '{"a":null,"z":[true,3],"é":"é"}'
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"registration_token": "secret"},
        {"nested": {"client-secret": "secret"}},
        {"value": 1.5},
        {"value": 1 << 54},
    ],
)
def test_canonical_json_rejects_secrets_and_unsafe_numbers(payload: dict[str, object]) -> None:
    with pytest.raises(AuditPayloadError):
        canonical_json(payload)


@pytest.mark.asyncio
async def test_append_and_verify_audit_chain(isolated_env: object) -> None:
    await ensure_schema()
    async with get_immediate_session() as session:
        project = Project(slug="audit-project", human_key="/audit-project")
        session.add(project)
        await session.flush()
        assert project.id is not None
        first = await append_audit_event(
            session,
            project_id=project.id,
            actor_kind="admin",
            actor_scope_id="admin/owner-1",
            actor_agent_id=None,
            operation_kind="project_created_v1",
            entity_type="project",
            entity_id=str(project.id),
            payload_version="project-created-v1",
            payload={"slug": project.slug},
            created_ts=datetime(2026, 7, 21, 20, 0, tzinfo=timezone.utc),
        )
        second = await append_audit_event(
            session,
            project_id=project.id,
            actor_kind="system",
            actor_scope_id="system/test-job",
            actor_agent_id=None,
            operation_kind="test_event_v1",
            entity_type="project",
            entity_id=str(project.id),
            payload_version="test-v1",
            payload={"count": 1},
            created_ts=datetime(2026, 7, 21, 20, 1, tzinfo=timezone.utc),
        )
        assert first.project_sequence == 1
        assert first.previous_event_hash == AUDIT_GENESIS_HASH
        assert second.project_sequence == 2
        assert second.previous_event_hash == first.event_hash
        await session.commit()

    async with get_session() as session:
        result = await verify_audit_chain(session, project.id)
    assert result.valid is True
    assert result.event_count == 2
    assert result.last_sequence == 2
    assert result.last_event_hash == second.event_hash


@pytest.mark.asyncio
async def test_audit_transaction_rollback_leaves_no_event_or_head(isolated_env: object) -> None:
    await ensure_schema()
    async with get_immediate_session() as session:
        project = Project(slug="rollback-project", human_key="/rollback-project")
        session.add(project)
        await session.flush()
        assert project.id is not None
        project_id = project.id
        await append_audit_event(
            session,
            project_id=project_id,
            actor_kind="system",
            actor_scope_id="system/rollback-test",
            actor_agent_id=None,
            operation_kind="rollback_test_v1",
            entity_type="project",
            entity_id=str(project_id),
            payload_version="test-v1",
            payload={},
        )
        await session.rollback()

    async with get_session() as session:
        event_count = (await session.execute(text("SELECT COUNT(*) FROM audit_events"))).scalar_one()
        head_count = (await session.execute(text("SELECT COUNT(*) FROM audit_heads"))).scalar_one()
    assert event_count == 0
    assert head_count == 0


@pytest.mark.asyncio
async def test_verify_audit_chain_detects_tampering(isolated_env: object) -> None:
    await ensure_schema()
    async with get_immediate_session() as session:
        project = Project(slug="tamper-project", human_key="/tamper-project")
        session.add(project)
        await session.flush()
        assert project.id is not None
        project_id = project.id
        event = await append_audit_event(
            session,
            project_id=project_id,
            actor_kind="system",
            actor_scope_id="system/tamper-test",
            actor_agent_id=None,
            operation_kind="tamper_test_v1",
            entity_type="project",
            entity_id=str(project_id),
            payload_version="test-v1",
            payload={"safe": True},
        )
        await session.commit()

    async with get_session() as session:
        await session.execute(
            text("UPDATE audit_events SET payload_json = :payload WHERE id = :event_id"),
            {"payload": '{"safe":false}', "event_id": event.id},
        )
        await session.commit()

    async with get_session() as session:
        result = await verify_audit_chain(session, project_id)
    assert result.valid is False
    assert result.error is not None
    assert "hash mismatch" in result.error
