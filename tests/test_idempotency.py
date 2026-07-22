from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from mcp_agent_mail.audit import append_audit_event
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session
from mcp_agent_mail.idempotency import (
    GENERIC_FINGERPRINT_VERSION,
    IdempotencyKeyReuseMismatchError,
    MutationReceipt,
    fingerprint_request,
    run_idempotent_mutation,
)
from mcp_agent_mail.models import Agent, AuditEvent, IdempotencyRecord, Message, Project


def _future() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)


async def _setup_sender() -> tuple[int, int]:
    async with get_immediate_session() as session:
        project = Project(slug="idempotency", human_key="/idempotency")
        session.add(project)
        await session.flush()
        assert project.id is not None
        agent = Agent(
            project_id=project.id,
            name="Sender",
            program="test",
            model="test",
        )
        session.add(agent)
        await session.flush()
        assert agent.id is not None
        await session.commit()
        return project.id, agent.id


def test_fingerprint_is_stable_for_equivalent_object_order() -> None:
    first = fingerprint_request(
        {"subject": "hello", "to": ["a", "b"]},
        version=GENERIC_FINGERPRINT_VERSION,
    )
    second = fingerprint_request(
        {"to": ["a", "b"], "subject": "hello"},
        version=GENERIC_FINGERPRINT_VERSION,
    )
    assert first == second


@pytest.mark.asyncio
async def test_commit_then_replay_returns_immutable_receipt(isolated_env: object) -> None:
    await ensure_schema()
    project_id, agent_id = await _setup_sender()
    calls = 0

    async def mutate(session) -> MutationReceipt:
        nonlocal calls
        calls += 1
        message = Message(
            project_id=project_id,
            sender_id=agent_id,
            subject="hello",
            body_md="body",
        )
        session.add(message)
        await session.flush()
        assert message.id is not None
        event = await append_audit_event(
            session,
            project_id=project_id,
            actor_kind="agent",
            actor_scope_id=f"agent/{project_id}:{agent_id}",
            actor_agent_id=agent_id,
            operation_kind="message_send_v1",
            entity_type="message",
            entity_id=str(message.id),
            payload_version="message-send-v1",
            payload={"message_id": message.id, "subject": message.subject},
        )
        assert event.id is not None
        return MutationReceipt(
            response={"message_id": message.id, "audit_event_id": event.id},
            entity_type="message",
            entity_id=str(message.id),
            project_id=project_id,
        )

    async with get_immediate_session() as session:
        first = await run_idempotent_mutation(
            session,
            scope_kind="agent",
            scope_id=f"{project_id}:{agent_id}",
            operation_kind="message_send_v1",
            idempotency_key="stable-key",
            request_payload={"subject": "hello", "body": "body"},
            expires_ts=_future(),
            mutate=mutate,
        )
        await session.commit()
    async with get_immediate_session() as session:
        replay = await run_idempotent_mutation(
            session,
            scope_kind="agent",
            scope_id=f"{project_id}:{agent_id}",
            operation_kind="message_send_v1",
            idempotency_key="stable-key",
            request_payload={"subject": "hello", "body": "body"},
            expires_ts=_future(),
            mutate=mutate,
        )
        await session.commit()

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response == first.response
    assert calls == 1


@pytest.mark.asyncio
async def test_key_reuse_mismatch_has_no_side_effects(isolated_env: object) -> None:
    await ensure_schema()
    project_id, agent_id = await _setup_sender()

    async def mutate(session) -> MutationReceipt:
        message = Message(
            project_id=project_id,
            sender_id=agent_id,
            subject="one",
            body_md="body",
        )
        session.add(message)
        await session.flush()
        assert message.id is not None
        return MutationReceipt(
            response={"message_id": message.id},
            entity_type="message",
            entity_id=str(message.id),
            project_id=project_id,
        )

    async with get_immediate_session() as session:
        await run_idempotent_mutation(
            session,
            scope_kind="agent",
            scope_id=f"{project_id}:{agent_id}",
            operation_kind="message_send_v1",
            idempotency_key="same-key",
            request_payload={"subject": "one"},
            expires_ts=_future(),
            mutate=mutate,
        )
        await session.commit()
    async with get_immediate_session() as session:
        with pytest.raises(IdempotencyKeyReuseMismatchError):
            await run_idempotent_mutation(
                session,
                scope_kind="agent",
                scope_id=f"{project_id}:{agent_id}",
                operation_kind="message_send_v1",
                idempotency_key="same-key",
                request_payload={"subject": "different"},
                expires_ts=_future(),
                mutate=mutate,
            )

    async with get_session() as session:
        message_count = await session.scalar(select(func.count()).select_from(Message))
        receipt_count = await session.scalar(select(func.count()).select_from(IdempotencyRecord))
    assert message_count == 1
    assert receipt_count == 1


@pytest.mark.asyncio
async def test_rollback_removes_domain_receipt_and_audit(isolated_env: object) -> None:
    await ensure_schema()
    project_id, agent_id = await _setup_sender()

    async def mutate(session) -> MutationReceipt:
        message = Message(
            project_id=project_id,
            sender_id=agent_id,
            subject="rollback",
            body_md="body",
        )
        session.add(message)
        await session.flush()
        assert message.id is not None
        await append_audit_event(
            session,
            project_id=project_id,
            actor_kind="agent",
            actor_scope_id=f"agent/{project_id}:{agent_id}",
            actor_agent_id=agent_id,
            operation_kind="message_send_v1",
            entity_type="message",
            entity_id=str(message.id),
            payload_version="message-send-v1",
            payload={"message_id": message.id},
        )
        return MutationReceipt(
            response={"message_id": message.id},
            entity_type="message",
            entity_id=str(message.id),
            project_id=project_id,
        )

    async with get_immediate_session() as session:
        await run_idempotent_mutation(
            session,
            scope_kind="agent",
            scope_id=f"{project_id}:{agent_id}",
            operation_kind="message_send_v1",
            idempotency_key="rollback-key",
            request_payload={"subject": "rollback"},
            expires_ts=_future(),
            mutate=mutate,
        )
        await session.rollback()

    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


@pytest.mark.asyncio
async def test_one_hundred_same_key_contenders_create_one_mutation(
    isolated_env: object,
) -> None:
    await ensure_schema()
    project_id, agent_id = await _setup_sender()

    async def contender() -> bool:
        async def mutate(session) -> MutationReceipt:
            message = Message(
                project_id=project_id,
                sender_id=agent_id,
                subject="concurrent",
                body_md="body",
            )
            session.add(message)
            await session.flush()
            assert message.id is not None
            event = await append_audit_event(
                session,
                project_id=project_id,
                actor_kind="agent",
                actor_scope_id=f"agent/{project_id}:{agent_id}",
                actor_agent_id=agent_id,
                operation_kind="message_send_v1",
                entity_type="message",
                entity_id=str(message.id),
                payload_version="message-send-v1",
                payload={"message_id": message.id},
            )
            return MutationReceipt(
                response={"message_id": message.id, "audit_event_id": event.id},
                entity_type="message",
                entity_id=str(message.id),
                project_id=project_id,
            )

        async with get_immediate_session() as session:
            result = await run_idempotent_mutation(
                session,
                scope_kind="agent",
                scope_id=f"{project_id}:{agent_id}",
                operation_kind="message_send_v1",
                idempotency_key="concurrent-key",
                request_payload={"subject": "concurrent", "body": "body"},
                expires_ts=_future(),
                mutate=mutate,
            )
            await session.commit()
            return result.replayed

    replayed = await asyncio.gather(*(contender() for _ in range(100)))
    assert replayed.count(False) == 1
    assert replayed.count(True) == 99
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
