"""Atomic, authenticated idempotency receipts for domain mutations.

This module never commits. Callers authenticate and authorize first, use an
immediate SQLite transaction, perform the domain mutation and audit append in
the callback, then commit the returned terminal receipt in the same boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import canonical_json
from .models import IdempotencyRecord

GENERIC_FINGERPRINT_VERSION: Final = "generic-json-v1"


class IdempotencyError(RuntimeError):
    """Base class for idempotency protocol failures."""


class IdempotencyKeyReuseMismatchError(IdempotencyError):
    """The key already names a request with a different canonical payload."""

    code: Final = "IDEMPOTENCY_KEY_REUSE_MISMATCH"


class IdempotencyVersionUnavailableError(IdempotencyError):
    """A retained record references a canonicalizer this binary cannot replay."""

    code: Final = "IDEMPOTENCY_VERSION_UNAVAILABLE"


class IdempotencyReceiptExpiredError(IdempotencyError):
    """The retry horizon has passed; the compact record still blocks reuse."""

    code: Final = "IDEMPOTENCY_RECEIPT_EXPIRED"


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    response: Mapping[str, Any]
    entity_type: str
    entity_id: str
    project_id: int | None


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    response: Mapping[str, Any]
    replayed: bool
    record: IdempotencyRecord


MutationCallback = Callable[[AsyncSession], Awaitable[MutationReceipt]]
Canonicalizer = Callable[[Mapping[str, Any]], str]


def _generic_canonicalizer(payload: Mapping[str, Any]) -> str:
    return canonical_json(payload)


_CANONICALIZERS: dict[str, Canonicalizer] = {
    GENERIC_FINGERPRINT_VERSION: _generic_canonicalizer,
}


def register_canonicalizer(version: str, canonicalizer: Canonicalizer) -> None:
    """Register a versioned canonicalizer without replacing retained behavior."""
    if not version:
        raise ValueError("canonicalizer version must not be empty")
    existing = _CANONICALIZERS.get(version)
    if existing is not None and existing is not canonicalizer:
        raise ValueError(f"canonicalizer version {version!r} is already registered")
    _CANONICALIZERS[version] = canonicalizer


def fingerprint_request(payload: Mapping[str, Any], *, version: str) -> str:
    canonicalizer = _CANONICALIZERS.get(version)
    if canonicalizer is None:
        raise IdempotencyVersionUnavailableError(
            f"canonicalizer version {version!r} is unavailable"
        )
    canonical = canonicalizer(payload)
    hash_input = f"agent-mail-idempotency\0{version}\0{canonical}".encode()
    return hashlib.sha256(hash_input).hexdigest()


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _find_record(
    session: AsyncSession,
    *,
    scope_kind: str,
    scope_id: str,
    operation_kind: str,
    idempotency_key: str,
) -> IdempotencyRecord | None:
    result = await session.execute(
        select(IdempotencyRecord).where(
            cast(Any, IdempotencyRecord.scope_kind) == scope_kind,
            cast(Any, IdempotencyRecord.scope_id) == scope_id,
            cast(Any, IdempotencyRecord.operation_kind) == operation_kind,
            cast(Any, IdempotencyRecord.idempotency_key) == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


async def lookup_idempotent_replay(
    session: AsyncSession,
    *,
    scope_kind: str,
    scope_id: str,
    operation_kind: str,
    idempotency_key: str,
    request_payload: Mapping[str, Any],
    now: datetime | None = None,
) -> IdempotencyResult | None:
    """Return a validated immutable receipt without running creation preflight."""
    existing = await _find_record(
        session,
        scope_kind=scope_kind,
        scope_id=scope_id,
        operation_kind=operation_kind,
        idempotency_key=idempotency_key,
    )
    if existing is None:
        return None
    candidate = fingerprint_request(request_payload, version=existing.fingerprint_version)
    if not hmac.compare_digest(candidate, existing.request_fingerprint):
        raise IdempotencyKeyReuseMismatchError(
            f"idempotency key was already used for a different {operation_kind} request"
        )
    timestamp = now or _utcnow_naive()
    if existing.expires_ts < timestamp:
        raise IdempotencyReceiptExpiredError(
            "idempotency receipt has expired; the key remains reserved against reuse"
        )
    response = json.loads(existing.response_json)
    if not isinstance(response, dict):
        raise IdempotencyError("stored idempotency receipt is not a JSON object")
    return IdempotencyResult(response=response, replayed=True, record=existing)


async def run_idempotent_mutation(
    session: AsyncSession,
    *,
    scope_kind: str,
    scope_id: str,
    operation_kind: str,
    idempotency_key: str,
    request_payload: Mapping[str, Any],
    expires_ts: datetime,
    mutate: MutationCallback,
    fingerprint_version: str = GENERIC_FINGERPRINT_VERSION,
    now: datetime | None = None,
) -> IdempotencyResult:
    """Run or replay one mutation without committing the caller's transaction."""
    timestamp = now or _utcnow_naive()
    replay = await lookup_idempotent_replay(
        session,
        scope_kind=scope_kind,
        scope_id=scope_id,
        operation_kind=operation_kind,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        now=timestamp,
    )
    if replay is not None:
        return replay

    fingerprint = fingerprint_request(request_payload, version=fingerprint_version)
    mutation = await mutate(session)
    response_json = canonical_json(mutation.response)
    record = IdempotencyRecord(
        scope_kind=scope_kind,
        scope_id=scope_id,
        project_id=mutation.project_id,
        operation_kind=operation_kind,
        idempotency_key=idempotency_key,
        fingerprint_version=fingerprint_version,
        request_fingerprint=fingerprint,
        response_json=response_json,
        entity_type=mutation.entity_type,
        entity_id=mutation.entity_id,
        created_ts=timestamp,
        expires_ts=expires_ts,
    )
    session.add(record)
    await session.flush()
    return IdempotencyResult(response=mutation.response, replayed=False, record=record)
