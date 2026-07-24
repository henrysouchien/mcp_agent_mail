from __future__ import annotations

import base64
import json

import pytest

from mcp_agent_mail.app import _FLEET_ERROR_CONTRACT_TOOLS
from mcp_agent_mail.error_contract import (
    ERROR_MARKER_PREFIX,
    ErrorContractError,
    build_error_envelope,
    decode_error_marker,
    encode_error_marker,
    validate_error_envelope,
)


def test_fleet_adapter_tool_set_has_one_strict_error_contract() -> None:
    assert {
        "activate_fleet_runtime",
        "agent_roster_current",
        "end_fleet_runtime_absent",
        "ensure_fleet_principal",
        "heartbeat_runtime_binding",
        "identity_status",
        "pre_stop_decision",
        "publish_fleet_launch_state",
        "publish_fleet_runtime_observation",
        "reconcile_runtime_binding",
    } == _FLEET_ERROR_CONTRACT_TOOLS


def test_error_marker_round_trip_preserves_reconciliation_contract() -> None:
    envelope = build_error_envelope(
        error_type="DATABASE_OPERATION_TIMEOUT",
        operation="ensure_fleet_principal",
        launch_attempt_id="11111111-1111-4111-8111-111111111111",
        error_id="22222222-2222-4222-8222-222222222222",
    )

    marker = encode_error_marker(envelope)
    decoded = decode_error_marker(
        f"Error calling tool 'ensure_fleet_principal': timed out {marker}"
    )

    assert marker.startswith(ERROR_MARKER_PREFIX)
    assert decoded == envelope
    assert decoded["retry_class"] == "reconcile"
    assert decoded["sanitized_context"] == {}


def test_unknown_type_is_stable_and_never_retried() -> None:
    envelope = build_error_envelope(
        error_type="SOMETHING_NEW",
        operation="activate_fleet_runtime",
    )

    assert envelope["message"] == "Agent Mail rejected the operation."
    assert envelope["retry_class"] == "never"
    assert envelope["recoverable"] is False


def test_malformed_or_incomplete_marker_fails_closed() -> None:
    with pytest.raises(ErrorContractError, match="length"):
        decode_error_marker(ERROR_MARKER_PREFIX)

    with pytest.raises(ErrorContractError, match="malformed"):
        decode_error_marker(f"{ERROR_MARKER_PREFIX}not_base64!")

    incomplete = {
        "schema_version": 1,
        "type": "TIMEOUT",
        "message": "timeout",
    }
    with pytest.raises(ErrorContractError, match="fields"):
        encode_error_marker(incomplete)


def test_public_contract_rejects_context_and_policy_contradictions() -> None:
    envelope = build_error_envelope(
        error_type="WINDOW_LOCATOR_ALREADY_ASSIGNED",
        operation="ensure_fleet_principal",
    )

    with pytest.raises(ErrorContractError, match="empty object"):
        validate_error_envelope(
            {
                **envelope,
                "sanitized_context": {"window_locator": "mcp-window:secret"},
            }
        )
    with pytest.raises(ErrorContractError, match="retry class contradicts"):
        validate_error_envelope({**envelope, "retry_class": "transient"})
    with pytest.raises(ErrorContractError, match="recoverable flag contradicts"):
        validate_error_envelope({**envelope, "recoverable": False})


def test_exception_and_authorization_material_never_enter_marker() -> None:
    envelope = build_error_envelope(
        error_type="WINDOW_LOCATOR_ALREADY_ASSIGNED",
        operation="ensure_fleet_principal",
    )
    marker = encode_error_marker(envelope)
    decoded_json = base64.urlsafe_b64decode(
        marker.removeprefix(ERROR_MARKER_PREFIX) + "=="
    ).decode()

    for secret in (
        "mcp-window:authorization-material",
        "Bearer secret",
        "sk-secret",
        "raw database exception",
    ):
        assert secret not in marker
        assert secret not in decoded_json
    assert json.loads(decoded_json)["sanitized_context"] == {}


def test_nonfinite_json_and_oversized_marker_fail_closed() -> None:
    envelope = build_error_envelope(
        error_type="TIMEOUT",
        operation="activate_fleet_runtime",
    )
    raw = json.dumps({**envelope, "sanitized_context": {"value": float("nan")}})
    encoded = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
    with pytest.raises(ErrorContractError, match="malformed"):
        decode_error_marker(f"{ERROR_MARKER_PREFIX}{encoded}")

    with pytest.raises(ErrorContractError, match="length"):
        decode_error_marker(f"{ERROR_MARKER_PREFIX}{'A' * 65_537}")
