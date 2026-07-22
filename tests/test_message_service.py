from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from mcp_agent_mail.audit import verify_audit_chain
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session
from mcp_agent_mail.message_service import RecipientIdentity, create_atomic_message
from mcp_agent_mail.models import Agent, AuditEvent, IdempotencyRecord, Message, MessageRecipient, Project


async def _identities() -> tuple[int, Agent, Agent, Agent]:
    async with get_immediate_session() as session:
        project = Project(slug="message-service", human_key="/message-service")
        session.add(project)
        await session.flush()
        assert project.id is not None
        agents = [
            Agent(
                project_id=project.id,
                name=name,
                program="test",
                model="test",
            )
            for name in ("Sender", "Recipient", "BlindRecipient")
        ]
        session.add_all(agents)
        await session.flush()
        await session.commit()
        assert all(agent.id is not None for agent in agents)
        return project.id, agents[0], agents[1], agents[2]


@pytest.mark.asyncio
async def test_message_domain_receipt_and_audit_commit_together(isolated_env: object) -> None:
    await ensure_schema()
    project_id, sender, recipient, blind = await _identities()
    assert sender.id is not None and recipient.id is not None and blind.id is not None
    recipients = [
        RecipientIdentity(recipient.id, recipient.name, "to"),
        RecipientIdentity(blind.id, blind.name, "bcc"),
    ]
    async with get_immediate_session() as session:
        result = await create_atomic_message(
            session,
            project_id=project_id,
            sender_id=sender.id,
            actor_scope_id=f"{project_id}:{sender.id}",
            recipients=recipients,
            subject="Atomic",
            body_md="One transaction",
            importance="normal",
            ack_required=False,
            thread_id=None,
            topic=None,
            reply_to=None,
            attachments=[],
            idempotency_key="message-key",
        )
        await session.commit()
    assert result.replayed is False
    assert result.response["audit_event_id"] is not None

    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(MessageRecipient)) == 2
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        chain = await verify_audit_chain(session, project_id)
        event = (await session.execute(select(AuditEvent))).scalar_one()
    assert chain.valid is True
    audit_payload = json.loads(event.payload_json)
    assert "Recipient" not in event.payload_json
    assert "BlindRecipient" not in event.payload_json
    assert audit_payload["recipient_count"] == 2


@pytest.mark.asyncio
async def test_same_message_key_replays_without_duplicate(isolated_env: object) -> None:
    await ensure_schema()
    project_id, sender, recipient, _blind = await _identities()
    assert sender.id is not None and recipient.id is not None
    sender_id = sender.id
    recipient_id = recipient.id

    async def call() -> tuple[bool, int]:
        async with get_immediate_session() as session:
            result = await create_atomic_message(
                session,
                project_id=project_id,
                sender_id=sender_id,
                actor_scope_id=f"{project_id}:{sender_id}",
                recipients=[RecipientIdentity(recipient_id, recipient.name, "to")],
                subject="Retry",
                body_md="Same request",
                importance="normal",
                ack_required=False,
                thread_id=None,
                topic=None,
                reply_to=None,
                attachments=[],
                idempotency_key="retry-key",
            )
            await session.commit()
            return result.replayed, result.message_id

    first = await call()
    replay = await call()
    assert first == (False, first[1])
    assert replay == (True, first[1])
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1


@pytest.mark.asyncio
async def test_keyless_atomic_message_reports_no_idempotency_record(isolated_env: object) -> None:
    await ensure_schema()
    project_id, sender, recipient, _blind = await _identities()
    assert sender.id is not None and recipient.id is not None
    async with get_immediate_session() as session:
        result = await create_atomic_message(
            session,
            project_id=project_id,
            sender_id=sender.id,
            actor_scope_id=f"{project_id}:{sender.id}",
            recipients=[RecipientIdentity(recipient.id, recipient.name, "to")],
            subject="Keyless",
            body_md="Still atomic",
            importance="normal",
            ack_required=False,
            thread_id=None,
            topic=None,
            reply_to=None,
            attachments=[],
            idempotency_key=None,
        )
        await session.commit()
    assert result.replayed is False
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


@pytest.mark.asyncio
async def test_message_service_rollback_removes_all_rows(isolated_env: object) -> None:
    await ensure_schema()
    project_id, sender, recipient, _blind = await _identities()
    assert sender.id is not None and recipient.id is not None
    async with get_immediate_session() as session:
        await create_atomic_message(
            session,
            project_id=project_id,
            sender_id=sender.id,
            actor_scope_id=f"{project_id}:{sender.id}",
            recipients=[RecipientIdentity(recipient.id, recipient.name, "to")],
            subject="Rollback",
            body_md="No rows survive",
            importance="normal",
            ack_required=False,
            thread_id=None,
            topic=None,
            reply_to=None,
            attachments=[],
            idempotency_key="rollback-message",
        )
        await session.rollback()
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0
