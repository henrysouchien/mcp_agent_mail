"""Versioned, token-safe error envelopes for MCP transport failures.

Version 1 intentionally carries no caller- or exception-supplied context.  A
future schema version may add typed public details, but arbitrary mappings are
not safe at an authentication boundary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

ERROR_MARKER_PREFIX = "AGENT_MAIL_ERROR_V1:"
ERROR_SCHEMA_VERSION = 1
MAX_ENCODED_MARKER_LENGTH = 65_536
MAX_OPERATION_LENGTH = 128

RetryClass = Literal[
    "transient",
    "idempotent_replay",
    "reconcile",
    "operator_action",
    "never",
]

_RETRY_CLASSES = {
    "transient",
    "idempotent_replay",
    "reconcile",
    "operator_action",
    "never",
}
_TYPE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_OPERATION_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")

_POLICY_PATH = Path(__file__).with_name("error_contract_v1.json")


def _load_policy() -> tuple[
    dict[str, RetryClass],
    dict[str, str],
    str,
    str,
]:
    raw = _POLICY_PATH.read_bytes()
    document = json.loads(raw)
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != ERROR_SCHEMA_VERSION
        or document.get("context_policy") != "empty_object"
    ):
        raise RuntimeError("Agent Mail error policy metadata is invalid")
    default = document.get("default")
    policies = document.get("policies")
    if not isinstance(default, dict) or not isinstance(policies, dict):
        raise RuntimeError("Agent Mail error policy table is invalid")
    default_message = default.get("message")
    default_retry = default.get("retry_class")
    if (
        not isinstance(default_message, str)
        or not default_message
        or default_retry != "never"
    ):
        raise RuntimeError("Agent Mail default error policy is invalid")
    retries: dict[str, RetryClass] = {}
    messages: dict[str, str] = {}
    for error_type, policy in policies.items():
        if (
            not isinstance(error_type, str)
            or not _TYPE_PATTERN.fullmatch(error_type)
            or not isinstance(policy, dict)
            or policy.get("retry_class") not in _RETRY_CLASSES
            or not isinstance(policy.get("message"), str)
            or not policy["message"]
        ):
            raise RuntimeError("Agent Mail typed error policy is invalid")
        retries[error_type] = cast(RetryClass, policy["retry_class"])
        messages[error_type] = policy["message"]
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return (
        retries,
        messages,
        default_message,
        hashlib.sha256(canonical).hexdigest(),
    )


(
    _RETRY_BY_TYPE,
    _MESSAGE_BY_TYPE,
    _DEFAULT_PUBLIC_MESSAGE,
    ERROR_POLICY_FINGERPRINT,
) = _load_policy()


class ErrorEnvelope(TypedDict):
    schema_version: int
    error_id: str
    type: str
    message: str
    recoverable: bool
    retry_class: RetryClass
    operation: str
    launch_attempt_id: str | None
    sanitized_context: dict[str, Any]


class ErrorContractError(ValueError):
    """Raised when an MCP error marker does not satisfy the public contract."""


def retry_class_for(error_type: str) -> RetryClass:
    """Return the server-owned retry policy for a stable error type."""
    normalized = _normalize_error_type(error_type)
    return _RETRY_BY_TYPE.get(normalized, "never")


def public_message_for(error_type: str) -> str:
    """Return public, stable prose that never contains exception text."""
    normalized = _normalize_error_type(error_type)
    return _MESSAGE_BY_TYPE.get(normalized, _DEFAULT_PUBLIC_MESSAGE)


def build_error_envelope(
    *,
    error_type: str,
    operation: str,
    launch_attempt_id: str | None = None,
    error_id: str | None = None,
) -> ErrorEnvelope:
    """Build a complete envelope from server-owned policy only."""
    normalized_type = _normalize_error_type(error_type)
    normalized_operation = _normalize_operation(operation)
    retry_class = retry_class_for(normalized_type)
    normalized_error_id = str(uuid.UUID(error_id)) if error_id else str(uuid.uuid4())
    normalized_attempt_id = (
        str(uuid.UUID(launch_attempt_id)) if launch_attempt_id else None
    )
    return {
        "schema_version": ERROR_SCHEMA_VERSION,
        "error_id": normalized_error_id,
        "type": normalized_type,
        "message": public_message_for(normalized_type),
        "recoverable": retry_class != "never",
        "retry_class": retry_class,
        "operation": normalized_operation,
        "launch_attempt_id": normalized_attempt_id,
        "sanitized_context": {},
    }


def encode_error_marker(envelope: Mapping[str, Any]) -> str:
    """Encode a validated envelope into a FastMCP-survivable text marker."""
    normalized = validate_error_envelope(envelope)
    raw = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{ERROR_MARKER_PREFIX}{encoded}"


def decode_error_marker(text: str) -> ErrorEnvelope:
    """Extract and validate the first versioned error marker in text."""
    marker_index = text.find(ERROR_MARKER_PREFIX)
    if marker_index < 0:
        raise ErrorContractError("versioned Agent Mail error marker is missing")
    remainder = text[marker_index + len(ERROR_MARKER_PREFIX) :].strip()
    encoded = remainder.split(maxsplit=1)[0] if remainder else ""
    if not encoded or len(encoded) > MAX_ENCODED_MARKER_LENGTH:
        raise ErrorContractError("Agent Mail error marker length is invalid")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ErrorContractError,
    ) as exc:
        raise ErrorContractError("Agent Mail error marker is malformed") from exc
    if not isinstance(payload, dict):
        raise ErrorContractError("Agent Mail error envelope must be an object")
    return validate_error_envelope(payload)


def validate_error_envelope(envelope: Mapping[str, Any]) -> ErrorEnvelope:
    """Validate the public error contract without inferring missing fields."""
    required = {
        "schema_version",
        "error_id",
        "type",
        "message",
        "recoverable",
        "retry_class",
        "operation",
        "launch_attempt_id",
        "sanitized_context",
    }
    if set(envelope) != required:
        raise ErrorContractError("Agent Mail error envelope fields are invalid")
    if (
        type(envelope["schema_version"]) is not int
        or envelope["schema_version"] != ERROR_SCHEMA_VERSION
    ):
        raise ErrorContractError("Agent Mail error schema version is unsupported")
    try:
        normalized_error_id = str(uuid.UUID(str(envelope["error_id"])))
    except ValueError as exc:
        raise ErrorContractError("Agent Mail error_id is invalid") from exc

    normalized_type = _normalize_error_type(envelope["type"])
    normalized_operation = _normalize_operation(envelope["operation"])
    retry_class = envelope["retry_class"]
    recoverable = envelope["recoverable"]
    expected_retry_class = retry_class_for(normalized_type)
    expected_message = public_message_for(normalized_type)
    if envelope["message"] != expected_message:
        raise ErrorContractError("Agent Mail public error message is invalid")
    if retry_class != expected_retry_class:
        raise ErrorContractError("Agent Mail retry class contradicts policy")
    if not isinstance(recoverable, bool):
        raise ErrorContractError("Agent Mail recoverable flag is invalid")
    if recoverable != (expected_retry_class != "never"):
        raise ErrorContractError("Agent Mail recoverable flag contradicts policy")
    if retry_class not in _RETRY_CLASSES:
        raise ErrorContractError("Agent Mail retry class is invalid")

    launch_attempt_id = envelope["launch_attempt_id"]
    if launch_attempt_id is not None:
        try:
            launch_attempt_id = str(uuid.UUID(str(launch_attempt_id)))
        except ValueError as exc:
            raise ErrorContractError(
                "Agent Mail launch_attempt_id is invalid"
            ) from exc
    context = envelope["sanitized_context"]
    if context != {}:
        raise ErrorContractError(
            "Agent Mail v1 sanitized_context must be an empty object"
        )
    return {
        "schema_version": ERROR_SCHEMA_VERSION,
        "error_id": normalized_error_id,
        "type": normalized_type,
        "message": expected_message,
        "recoverable": recoverable,
        "retry_class": cast(RetryClass, retry_class),
        "operation": normalized_operation,
        "launch_attempt_id": launch_attempt_id,
        "sanitized_context": {},
    }


def _normalize_error_type(value: Any) -> str:
    if not isinstance(value, str):
        raise ErrorContractError("Agent Mail error type is invalid")
    normalized = value.strip().upper()
    if not _TYPE_PATTERN.fullmatch(normalized):
        raise ErrorContractError("Agent Mail error type is invalid")
    return normalized


def _normalize_operation(value: Any) -> str:
    if not isinstance(value, str):
        raise ErrorContractError("Agent Mail error operation is invalid")
    normalized = value.strip()
    if (
        len(normalized) > MAX_OPERATION_LENGTH
        or not _OPERATION_PATTERN.fullmatch(normalized)
    ):
        raise ErrorContractError("Agent Mail error operation is invalid")
    return normalized


def _reject_json_constant(value: str) -> None:
    raise ErrorContractError(f"non-finite JSON number is forbidden: {value}")
