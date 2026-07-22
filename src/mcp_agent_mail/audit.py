"""Canonical, tamper-evident audit-ledger primitives.

The functions in this module never commit a transaction. Callers add the
domain mutation, idempotency receipt, and audit event to one SQLite transaction
and own the final commit or rollback boundary.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditEvent, AuditHead

AUDIT_HASH_VERSION: Final = "audit-json-v1"
AUDIT_GENESIS_HASH: Final = "0" * 64
_JCS_SAFE_INTEGER_MAX: Final = (1 << 53) - 1
_SENSITIVE_KEY_PARTS: Final = (
    "authorization",
    "bearer",
    "password",
    "private_key",
    "registration_token",
    "secret",
    "sender_token",
)


class AuditPayloadError(ValueError):
    """Raised when an event cannot be represented safely and canonically."""


@dataclass(frozen=True, slots=True)
class AuditVerificationResult:
    valid: bool
    event_count: int
    last_sequence: int
    last_event_hash: str
    error: str | None = None


def _normalize_timestamp(value: datetime) -> str:
    normalized = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _normalize_jcs_value(value: Any, *, path: str = "$") -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, int):
        if not -_JCS_SAFE_INTEGER_MAX <= value <= _JCS_SAFE_INTEGER_MAX:
            raise AuditPayloadError(f"{path}: integer exceeds the JCS safe range; encode it as a string")
        return value
    if isinstance(value, float):
        raise AuditPayloadError(f"{path}: floats are forbidden; encode normalized decimal values as strings")
    if isinstance(value, Mapping):
        normalized_items: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise AuditPayloadError(f"{path}: object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if _is_sensitive_key(key):
                raise AuditPayloadError(f"{path}.{key}: secret-bearing fields are forbidden in audit payloads")
            if key in normalized_items:
                raise AuditPayloadError(f"{path}: key collision after NFC normalization: {key!r}")
            normalized_items[key] = _normalize_jcs_value(raw_value, path=f"{path}.{key}")
        return {
            key: normalized_items[key]
            for key in sorted(normalized_items, key=lambda item: item.encode("utf-16be"))
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        return [_normalize_jcs_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise AuditPayloadError(f"{path}: unsupported audit payload type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return the supported RFC 8785/JCS subset as canonical UTF-8 JSON text."""
    normalized = _normalize_jcs_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _event_hash_input(
    *,
    project_id: int,
    project_sequence: int,
    actor_kind: str,
    actor_scope_id: str,
    actor_agent_id: int | None,
    operation_kind: str,
    entity_type: str,
    entity_id: str,
    payload_version: str,
    payload_json: str,
    previous_event_hash: str,
    created_ts: datetime,
) -> dict[str, Any]:
    return {
        "actor_agent_id": actor_agent_id,
        "actor_kind": actor_kind,
        "actor_scope_id": actor_scope_id,
        "created_ts": _normalize_timestamp(created_ts),
        "entity_id": entity_id,
        "entity_type": entity_type,
        "hash_version": AUDIT_HASH_VERSION,
        "operation_kind": operation_kind,
        "payload_json": payload_json,
        "payload_version": payload_version,
        "previous_event_hash": previous_event_hash,
        "project_id": project_id,
        "project_sequence": project_sequence,
    }


def compute_event_hash(**fields: Any) -> str:
    hash_input = _event_hash_input(**fields)
    return hashlib.sha256(canonical_json(hash_input).encode("utf-8")).hexdigest()


async def append_audit_event(
    session: AsyncSession,
    *,
    project_id: int,
    actor_kind: str,
    actor_scope_id: str,
    actor_agent_id: int | None,
    operation_kind: str,
    entity_type: str,
    entity_id: str,
    payload_version: str,
    payload: Mapping[str, Any],
    created_ts: datetime | None = None,
) -> AuditEvent:
    """Append an event and advance its project head without committing.

    The caller must use a transaction that serializes the head read and update
    (``get_immediate_session`` for SQLite) and commit the domain mutation in the
    same transaction.
    """
    timestamp = created_ts or datetime.now(timezone.utc).replace(tzinfo=None)
    payload_json = canonical_json(payload)
    result = await session.execute(
        select(AuditHead).where(cast(Any, AuditHead.project_id) == project_id)
    )
    head = result.scalar_one_or_none()
    previous_hash = head.last_event_hash if head is not None else AUDIT_GENESIS_HASH
    sequence = (head.last_sequence if head is not None else 0) + 1
    fields = {
        "project_id": project_id,
        "project_sequence": sequence,
        "actor_kind": actor_kind,
        "actor_scope_id": actor_scope_id,
        "actor_agent_id": actor_agent_id,
        "operation_kind": operation_kind,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "payload_version": payload_version,
        "payload_json": payload_json,
        "previous_event_hash": previous_hash,
        "created_ts": timestamp,
    }
    event = AuditEvent(**fields, event_hash=compute_event_hash(**fields))
    session.add(event)
    if head is None:
        head = AuditHead(
            project_id=project_id,
            last_sequence=sequence,
            last_event_hash=event.event_hash,
        )
        session.add(head)
    else:
        head.last_sequence = sequence
        head.last_event_hash = event.event_hash
    await session.flush()
    return event


async def verify_audit_chain(session: AsyncSession, project_id: int) -> AuditVerificationResult:
    result = await session.execute(
        select(AuditEvent)
        .where(cast(Any, AuditEvent.project_id) == project_id)
        .order_by(cast(Any, AuditEvent.project_sequence))
    )
    events = list(result.scalars())
    head_result = await session.execute(
        select(AuditHead).where(cast(Any, AuditHead.project_id) == project_id)
    )
    head = head_result.scalar_one_or_none()
    previous_hash = AUDIT_GENESIS_HASH
    for expected_sequence, event in enumerate(events, start=1):
        if event.project_sequence != expected_sequence:
            return AuditVerificationResult(
                False,
                len(events),
                expected_sequence - 1,
                previous_hash,
                f"expected sequence {expected_sequence}, found {event.project_sequence}",
            )
        if event.previous_event_hash != previous_hash:
            return AuditVerificationResult(
                False,
                len(events),
                expected_sequence - 1,
                previous_hash,
                f"event {event.id} has the wrong previous hash",
            )
        fields = {
            "project_id": event.project_id,
            "project_sequence": event.project_sequence,
            "actor_kind": event.actor_kind,
            "actor_scope_id": event.actor_scope_id,
            "actor_agent_id": event.actor_agent_id,
            "operation_kind": event.operation_kind,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "payload_version": event.payload_version,
            "payload_json": event.payload_json,
            "previous_event_hash": event.previous_event_hash,
            "created_ts": event.created_ts,
        }
        expected_hash = compute_event_hash(**fields)
        if event.event_hash != expected_hash:
            return AuditVerificationResult(
                False,
                len(events),
                expected_sequence - 1,
                previous_hash,
                f"event {event.id} hash mismatch",
            )
        previous_hash = event.event_hash
    last_sequence = len(events)
    if head is None:
        if events:
            return AuditVerificationResult(False, len(events), last_sequence, previous_hash, "head is missing")
        return AuditVerificationResult(True, 0, 0, AUDIT_GENESIS_HASH)
    if head.last_sequence != last_sequence or head.last_event_hash != previous_hash:
        return AuditVerificationResult(False, len(events), last_sequence, previous_hash, "head mismatch")
    return AuditVerificationResult(True, len(events), last_sequence, previous_hash)
