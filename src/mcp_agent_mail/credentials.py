"""Secret-backed pane and bootstrap credential primitives.

Callers must perform owner/admin authorization before creation, rotation,
revocation, or reassignment. These helpers never commit and never persist or
log a plaintext secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from .models import BootstrapCredential, PaneCredential

_PANE_DOMAIN: Final = b"pane-credential-v1"
_BOOTSTRAP_DOMAIN: Final = b"bootstrap-credential-v1"
_BOOTSTRAP_PANE_DOMAIN: Final = b"bootstrap-pane-derivation-v1"
_BOOTSTRAP_REGISTRATION_DOMAIN: Final = b"bootstrap-registration-derivation-v1"
_SECRET_BYTES: Final = 32


class CredentialError(RuntimeError):
    """Base class for credential failures safe to map to authentication errors."""


class MalformedCredentialError(CredentialError):
    """The bearer cannot be parsed into its versioned wire format."""


class InvalidCredentialError(CredentialError):
    """The credential is unknown, expired, revoked, consumed, or mismatched."""


class PepperUnavailableError(CredentialError):
    """The verifier's named server pepper is unavailable; fail closed."""


class CredentialGenerationConflictError(CredentialError):
    """A rotate request used a stale compare-and-swap generation."""


@dataclass(frozen=True, slots=True)
class IssuedPaneCredential:
    record: PaneCredential
    bearer: str


@dataclass(frozen=True, slots=True)
class IssuedBootstrapCredential:
    record: BootstrapCredential
    bearer: str


@dataclass(frozen=True, slots=True)
class DerivedRegistrationCredentials:
    pane: IssuedPaneCredential
    registration_token: str


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _encode_secret(secret: bytes) -> str:
    return base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")


def _decode_secret(encoded: str) -> bytes:
    try:
        padding = "=" * (-len(encoded) % 4)
        secret = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise MalformedCredentialError("credential secret is not canonical base64url") from exc
    if len(secret) != _SECRET_BYTES or _encode_secret(secret) != encoded:
        raise MalformedCredentialError("credential secret has invalid length or encoding")
    return secret


def _pepper(peppers: Mapping[str, bytes], key_id: str) -> bytes:
    pepper = peppers.get(key_id)
    if pepper is None:
        raise PepperUnavailableError(f"server pepper {key_id!r} is unavailable")
    if len(pepper) < _SECRET_BYTES:
        raise PepperUnavailableError(f"server pepper {key_id!r} is too short")
    return pepper


def _pane_verifier_input(credential_id: str, generation: int, secret: bytes) -> bytes:
    encoded_id = credential_id.encode("utf-8")
    return (
        _PANE_DOMAIN
        + b"\0"
        + struct.pack(">I", len(encoded_id))
        + encoded_id
        + struct.pack(">Q", generation)
        + secret
    )


def _bootstrap_verifier_input(credential_id: str, secret: bytes) -> bytes:
    encoded_id = credential_id.encode("utf-8")
    return (
        _BOOTSTRAP_DOMAIN
        + b"\0"
        + struct.pack(">I", len(encoded_id))
        + encoded_id
        + secret
    )


def _digest(pepper: bytes, verifier_input: bytes) -> str:
    return hmac.new(pepper, verifier_input, hashlib.sha256).hexdigest()


def _parse_pane_bearer(bearer: str) -> tuple[str, int, bytes]:
    try:
        credential_id, generation_raw, encoded_secret = bearer.split(".", 2)
        generation = int(generation_raw)
    except (ValueError, AttributeError) as exc:
        raise MalformedCredentialError("pane credential has invalid wire format") from exc
    if not credential_id or generation < 1:
        raise MalformedCredentialError("pane credential has invalid identity or generation")
    return credential_id, generation, _decode_secret(encoded_secret)


def _parse_bootstrap_bearer(bearer: str) -> tuple[str, bytes]:
    try:
        credential_id, encoded_secret = bearer.split(".", 1)
    except (ValueError, AttributeError) as exc:
        raise MalformedCredentialError("bootstrap credential has invalid wire format") from exc
    if not credential_id:
        raise MalformedCredentialError("bootstrap credential has no identity")
    return credential_id, _decode_secret(encoded_secret)


async def create_pane_credential(
    session: AsyncSession,
    *,
    project_id: int,
    agent_id: int,
    window_uuid: str,
    pepper_key_id: str,
    peppers: Mapping[str, bytes],
    expires_ts: datetime | None = None,
) -> IssuedPaneCredential:
    credential_id = uuid.uuid4().hex
    secret = secrets.token_bytes(_SECRET_BYTES)
    generation = 1
    record = PaneCredential(
        id=credential_id,
        project_id=project_id,
        agent_id=agent_id,
        window_uuid=window_uuid,
        secret_digest=_digest(
            _pepper(peppers, pepper_key_id),
            _pane_verifier_input(credential_id, generation, secret),
        ),
        pepper_key_id=pepper_key_id,
        generation=generation,
        expires_ts=expires_ts,
    )
    session.add(record)
    await session.flush()
    return IssuedPaneCredential(
        record=record,
        bearer=f"{credential_id}.{generation}.{_encode_secret(secret)}",
    )


async def verify_pane_credential(
    session: AsyncSession,
    bearer: str,
    *,
    peppers: Mapping[str, bytes],
    now: datetime | None = None,
) -> PaneCredential:
    credential_id, generation, secret = _parse_pane_bearer(bearer)
    record = await session.get(PaneCredential, credential_id)
    timestamp = now or _utcnow_naive()
    if record is None or record.revoked_ts is not None:
        raise InvalidCredentialError("pane credential is invalid")
    if record.expires_ts is not None and record.expires_ts < timestamp:
        raise InvalidCredentialError("pane credential is invalid")
    expected = _digest(
        _pepper(peppers, record.pepper_key_id),
        _pane_verifier_input(record.id, generation, secret),
    )
    if generation != record.generation or not hmac.compare_digest(expected, record.secret_digest):
        raise InvalidCredentialError("pane credential is invalid")
    record.last_used_ts = timestamp
    await session.flush()
    return record


async def rotate_pane_credential(
    session: AsyncSession,
    credential_id: str,
    *,
    expected_generation: int,
    pepper_key_id: str,
    peppers: Mapping[str, bytes],
) -> IssuedPaneCredential:
    record = await session.get(PaneCredential, credential_id)
    if record is None or record.revoked_ts is not None:
        raise InvalidCredentialError("pane credential is invalid")
    if record.generation != expected_generation:
        raise CredentialGenerationConflictError("pane credential generation changed")
    secret = secrets.token_bytes(_SECRET_BYTES)
    record.generation += 1
    record.pepper_key_id = pepper_key_id
    record.secret_digest = _digest(
        _pepper(peppers, pepper_key_id),
        _pane_verifier_input(record.id, record.generation, secret),
    )
    await session.flush()
    return IssuedPaneCredential(
        record=record,
        bearer=f"{record.id}.{record.generation}.{_encode_secret(secret)}",
    )


async def revoke_pane_credential(
    session: AsyncSession,
    credential_id: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> PaneCredential:
    record = await session.get(PaneCredential, credential_id)
    if record is None:
        raise InvalidCredentialError("pane credential is invalid")
    if record.revoked_ts is None:
        record.revoked_ts = now or _utcnow_naive()
        record.revoke_reason = reason
        await session.flush()
    return record


async def create_bootstrap_credential(
    session: AsyncSession,
    *,
    project_id: int | None,
    prospective_project_digest: str | None,
    window_uuid: str,
    pepper_key_id: str,
    peppers: Mapping[str, bytes],
    expires_ts: datetime,
) -> IssuedBootstrapCredential:
    if (project_id is None) == (prospective_project_digest is None):
        raise ValueError("exactly one project scope must be supplied")
    credential_id = uuid.uuid4().hex
    secret = secrets.token_bytes(_SECRET_BYTES)
    record = BootstrapCredential(
        id=credential_id,
        project_id=project_id,
        prospective_project_digest=prospective_project_digest,
        secret_digest=_digest(
            _pepper(peppers, pepper_key_id),
            _bootstrap_verifier_input(credential_id, secret),
        ),
        pepper_key_id=pepper_key_id,
        window_uuid=window_uuid,
        expires_ts=expires_ts,
    )
    session.add(record)
    await session.flush()
    return IssuedBootstrapCredential(
        record=record,
        bearer=f"{credential_id}.{_encode_secret(secret)}",
    )


async def verify_bootstrap_credential(
    session: AsyncSession,
    bearer: str,
    *,
    peppers: Mapping[str, bytes],
    window_uuid: str,
    allow_consumed_idempotency_key: str | None = None,
    now: datetime | None = None,
) -> BootstrapCredential:
    credential_id, secret = _parse_bootstrap_bearer(bearer)
    record = await session.get(BootstrapCredential, credential_id)
    timestamp = now or _utcnow_naive()
    if (
        record is None
        or record.revoked_ts is not None
        or (
            record.consumed_ts is not None
            and record.consumed_idempotency_key != allow_consumed_idempotency_key
        )
        or record.expires_ts < timestamp
        or record.window_uuid != window_uuid
    ):
        raise InvalidCredentialError("bootstrap credential is invalid")
    expected = _digest(
        _pepper(peppers, record.pepper_key_id),
        _bootstrap_verifier_input(record.id, secret),
    )
    if not hmac.compare_digest(expected, record.secret_digest):
        raise InvalidCredentialError("bootstrap credential is invalid")
    return record


async def derive_registration_credentials(
    session: AsyncSession,
    bootstrap_bearer: str,
    *,
    project_id: int,
    agent_id: int,
    window_uuid: str,
    idempotency_key: str,
    pepper_key_id: str,
    peppers: Mapping[str, bytes],
) -> DerivedRegistrationCredentials:
    """Deterministically mint replayable secrets from one verified bootstrap bearer."""
    bootstrap_id, bootstrap_secret = _parse_bootstrap_bearer(bootstrap_bearer)
    context = (
        f"{project_id}\0{agent_id}\0{window_uuid}\0{idempotency_key}".encode("utf-8")
    )
    pane_secret = hmac.new(
        bootstrap_secret,
        _BOOTSTRAP_PANE_DOMAIN + b"\0" + context,
        hashlib.sha256,
    ).digest()
    registration_secret = hmac.new(
        bootstrap_secret,
        _BOOTSTRAP_REGISTRATION_DOMAIN + b"\0" + context,
        hashlib.sha256,
    ).digest()
    credential_id = hashlib.sha256(
        _BOOTSTRAP_PANE_DOMAIN + b"\0" + bootstrap_id.encode("utf-8")
    ).hexdigest()[:32]
    generation = 1
    secret_digest = _digest(
        _pepper(peppers, pepper_key_id),
        _pane_verifier_input(credential_id, generation, pane_secret),
    )
    record = await session.get(PaneCredential, credential_id)
    if record is None:
        record = PaneCredential(
            id=credential_id,
            project_id=project_id,
            agent_id=agent_id,
            window_uuid=window_uuid,
            secret_digest=secret_digest,
            pepper_key_id=pepper_key_id,
            generation=generation,
        )
        session.add(record)
        await session.flush()
    elif (
        record.project_id != project_id
        or record.agent_id != agent_id
        or record.window_uuid != window_uuid
        or record.generation != generation
        or record.pepper_key_id != pepper_key_id
        or not hmac.compare_digest(record.secret_digest, secret_digest)
    ):
        raise InvalidCredentialError("bootstrap-derived pane credential conflicts")
    return DerivedRegistrationCredentials(
        pane=IssuedPaneCredential(
            record=record,
            bearer=f"{credential_id}.{generation}.{_encode_secret(pane_secret)}",
        ),
        registration_token=_encode_secret(registration_secret),
    )


async def consume_bootstrap_credential(
    session: AsyncSession,
    record: BootstrapCredential,
    *,
    agent_id: int,
    idempotency_key: str,
    now: datetime | None = None,
) -> None:
    if record.consumed_ts is not None:
        raise InvalidCredentialError("bootstrap credential is already consumed")
    record.consumed_ts = now or _utcnow_naive()
    record.consumed_agent_id = agent_id
    record.consumed_idempotency_key = idempotency_key
    await session.flush()
