"""Git-independent atomic message mutation service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from .audit import append_audit_event
from .idempotency import IdempotencyResult, MutationReceipt, run_idempotent_mutation
from .models import Agent, Message, MessageRecipient


@dataclass(frozen=True, slots=True)
class RecipientIdentity:
    agent_id: int
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class AtomicMessageResult:
    response: Mapping[str, Any]
    replayed: bool
    message_id: int


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return normalized.astimezone(timezone.utc).isoformat()


def _request_payload(
    *,
    project_id: int,
    sender_id: int,
    recipients: Sequence[RecipientIdentity],
    subject: str,
    body_md: str,
    importance: str,
    ack_required: bool,
    thread_id: str | None,
    topic: str | None,
    reply_to: int | None,
    attachments: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ack_required": ack_required,
        "attachments": list(attachments),
        "body_md": body_md,
        "importance": importance,
        "project_id": project_id,
        "recipients": [
            {"agent_id": recipient.agent_id, "kind": recipient.kind}
            for recipient in recipients
        ],
        "reply_to": reply_to,
        "sender_id": sender_id,
        "subject": subject,
        "thread_id": thread_id,
        "topic": topic,
    }


async def create_atomic_message(
    session: AsyncSession,
    *,
    project_id: int,
    sender_id: int,
    actor_scope_id: str,
    recipients: Sequence[RecipientIdentity],
    subject: str,
    body_md: str,
    importance: str,
    ack_required: bool,
    thread_id: str | None,
    topic: str | None,
    reply_to: int | None,
    attachments: Sequence[dict[str, Any]],
    idempotency_key: str | None,
    retry_horizon: timedelta = timedelta(days=30),
) -> AtomicMessageResult:
    """Create a message, recipients, audit event, and optional receipt atomically."""
    payload = _request_payload(
        project_id=project_id,
        sender_id=sender_id,
        recipients=recipients,
        subject=subject,
        body_md=body_md,
        importance=importance,
        ack_required=ack_required,
        thread_id=thread_id,
        topic=topic,
        reply_to=reply_to,
        attachments=attachments,
    )

    async def mutate(transaction: AsyncSession) -> MutationReceipt:
        sender = await transaction.get(Agent, sender_id)
        if sender is None:
            raise ValueError(f"sender agent {sender_id} does not exist")
        message = Message(
            project_id=project_id,
            sender_id=sender_id,
            subject=subject,
            body_md=body_md,
            importance=importance,
            ack_required=ack_required,
            thread_id=thread_id,
            topic=topic,
            reply_to=reply_to,
            attachments=list(attachments),
        )
        transaction.add(message)
        await transaction.flush()
        if message.id is None:
            raise RuntimeError("message id was not allocated")
        for recipient in recipients:
            transaction.add(
                MessageRecipient(
                    message_id=message.id,
                    agent_id=recipient.agent_id,
                    kind=recipient.kind,
                )
            )
        sender.last_active_ts = _utcnow_naive()
        event = await append_audit_event(
            transaction,
            project_id=project_id,
            actor_kind="agent",
            actor_scope_id=actor_scope_id,
            actor_agent_id=sender_id,
            operation_kind="message_reply_v1" if reply_to is not None else "message_send_v1",
            entity_type="message",
            entity_id=str(message.id),
            payload_version="message-mutation-v1",
            payload={
                "ack_required": ack_required,
                "attachment_count": len(attachments),
                "importance": importance,
                "message_id": message.id,
                "recipient_count": len(recipients),
                "reply_to": reply_to,
                "thread_id": thread_id,
                "topic": topic,
            },
        )
        response: dict[str, Any] = {
            "id": message.id,
            "project_id": project_id,
            "thread_id": thread_id,
            "topic": topic,
            "subject": subject,
            "importance": importance,
            "ack_required": ack_required,
            "created_ts": _iso(message.created_ts),
            "audit_event_id": event.id,
            "event_hash": event.event_hash,
        }
        if reply_to is not None:
            response["reply_to"] = reply_to
        return MutationReceipt(
            response=response,
            entity_type="message",
            entity_id=str(message.id),
            project_id=project_id,
        )

    if idempotency_key:
        operation_kind = "message_reply_v1" if reply_to is not None else "message_send_v1"
        result: IdempotencyResult = await run_idempotent_mutation(
            session,
            scope_kind="agent",
            scope_id=actor_scope_id,
            operation_kind=operation_kind,
            idempotency_key=idempotency_key,
            request_payload=payload,
            expires_ts=_utcnow_naive() + retry_horizon,
            mutate=mutate,
        )
        message_id = int(result.response["id"])
        return AtomicMessageResult(result.response, result.replayed, message_id)

    mutation = await mutate(session)
    return AtomicMessageResult(mutation.response, False, int(mutation.entity_id))
