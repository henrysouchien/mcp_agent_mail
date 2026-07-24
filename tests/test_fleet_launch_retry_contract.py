from __future__ import annotations

import hashlib
import uuid
from typing import Any, cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.error_contract import decode_error_marker
from mcp_agent_mail.models import (
    Agent,
    LogicalAgentPrincipal,
    Project,
    RuntimeBinding,
    WindowIdentity,
)


def configure_database_profile(
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner_token: str,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    monkeypatch.setenv("CORE_OWNER_TOKEN", owner_token)
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    _config.clear_settings_cache()


def ensure_args(
    *,
    project_key: str,
    logical_key: str,
    launch_attempt_id: str,
    window_locator: str,
    owner_token: str,
    supervisor_sequence: int,
    proof_mode: str = "create",
    expected_agent_id: int | None = None,
    agent_recovery_authority: str | None = None,
    recovery_launch_attempt_id: str | None = None,
) -> dict[str, Any]:
    project_hash = hashlib.sha256(project_key.encode()).hexdigest()
    return {
        "canonical_project": project_key,
        "logical_agent_key": logical_key,
        "launch_attempt_id": launch_attempt_id,
        "proof_mode": proof_mode,
        "window_locator": window_locator,
        "provider": "claude",
        "requested_model": "fable",
        "task_description": "same-workload retry regression",
        "owner_token": owner_token,
        "idempotency_key": (
            f"ensure-principal:v1:{project_hash}:{logical_key}:"
            f"{launch_attempt_id}"
        ),
        "supervisor_sequence": supervisor_sequence,
        "expected_generation": 0,
        "expected_agent_id": expected_agent_id,
        "agent_recovery_authority": agent_recovery_authority,
        "recovery_launch_attempt_id": recovery_launch_attempt_id,
    }


@pytest.mark.asyncio
async def test_locator_conflict_is_typed_and_creates_no_ghost_principal(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = "test-fleet-controller-owner"
    configure_database_profile(monkeypatch, owner_token=owner_token)
    project_key = "/regression/fleet-locator-preflight"
    window_locator = str(uuid.uuid4())
    first_key = f"fa:{uuid.uuid4()}"
    second_key = f"fa:{uuid.uuid4()}"

    async with Client(build_mcp_server()) as client:
        await client.call_tool(
            "ensure_fleet_principal",
            ensure_args(
                project_key=project_key,
                logical_key=first_key,
                launch_attempt_id=str(uuid.uuid4()),
                window_locator=window_locator,
                owner_token=owner_token,
                supervisor_sequence=1,
            ),
        )
        with pytest.raises(ToolError) as failed:
            await client.call_tool(
                "ensure_fleet_principal",
                ensure_args(
                    project_key=project_key,
                    logical_key=second_key,
                    launch_attempt_id=str(uuid.uuid4()),
                    window_locator=window_locator,
                    owner_token=owner_token,
                    supervisor_sequence=1,
                ),
            )

    envelope = decode_error_marker(str(failed.value))
    assert envelope["type"] == "WINDOW_LOCATOR_ALREADY_ASSIGNED"
    assert envelope["retry_class"] == "operator_action"
    async with get_session() as session:
        project = (
            await session.execute(
                select(Project).where(
                    cast(Any, Project.human_key) == project_key
                )
            )
        ).scalar_one()
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Agent)
                .where(cast(Any, Agent.project_id) == project.id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(LogicalAgentPrincipal)
                .where(cast(Any, LogicalAgentPrincipal.project_id) == project.id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_failed_phase_a_retries_same_authority_with_new_attempt(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = "test-fleet-controller-owner"
    configure_database_profile(monkeypatch, owner_token=owner_token)
    project_key = "/regression/fleet-same-workload-retry"
    logical_key = f"fa:{uuid.uuid4()}"
    first_attempt = str(uuid.uuid4())
    retry_attempt = str(uuid.uuid4())
    window_locator = str(uuid.uuid4())

    async with Client(build_mcp_server()) as client:
        first = await client.call_tool(
            "ensure_fleet_principal",
            ensure_args(
                project_key=project_key,
                logical_key=logical_key,
                launch_attempt_id=first_attempt,
                window_locator=window_locator,
                owner_token=owner_token,
                supervisor_sequence=1,
            ),
        )
        await client.call_tool(
            "publish_fleet_launch_state",
            {
                "project_key": project_key,
                "logical_agent_key": logical_key,
                "desired_state": "held",
                "coordination_state": "failed",
                "supervisor_sequence": 2,
                "launch_attempt_id": first_attempt,
                "identity_context_injected": False,
                "owner_token": owner_token,
            },
        )
        retried = await client.call_tool(
            "ensure_fleet_principal",
            ensure_args(
                project_key=project_key,
                logical_key=logical_key,
                launch_attempt_id=retry_attempt,
                window_locator=window_locator,
                owner_token=owner_token,
                supervisor_sequence=3,
                proof_mode="retry_failed_create",
                expected_agent_id=first.data["agent_id"],
                agent_recovery_authority=first.data[
                    "agent_recovery_authority"
                ],
                recovery_launch_attempt_id=first_attempt,
            ),
        )

        runtime_incarnation_id = str(uuid.uuid4())
        project_hash = hashlib.sha256(project_key.encode()).hexdigest()
        activated = await client.call_tool(
            "activate_fleet_runtime",
            {
                "canonical_project": project_key,
                "logical_agent_key": logical_key,
                "launch_attempt_id": retry_attempt,
                "phase_a_launch_receipt": retried.data["launch_receipt"],
                "proof_mode": "retry_failed_create",
                "agent_id": first.data["agent_id"],
                "runtime_session_id": str(uuid.uuid4()),
                "runtime_incarnation_id": runtime_incarnation_id,
                "pane_instance_id": str(uuid.uuid4()),
                "process_id": 5101,
                "host_boot_id": "boot-fleet",
                "host_id": "host-fleet",
                "tmux_server_id": "server-fleet",
                "pane_id": "%51",
                "program": "claude",
                "model": "fable",
                "task_description": "same-workload retry regression",
                "process_started_ts": "2026-07-23T14:00:00Z",
                "expected_generation": 0,
                "owner_token": owner_token,
                "agent_recovery_authority": first.data[
                    "agent_recovery_authority"
                ],
                "idempotency_key": (
                    f"activate-runtime:v1:{project_hash}:{logical_key}:"
                    f"{retry_attempt}:{runtime_incarnation_id}"
                ),
                "supervisor_sequence": 4,
            },
        )

    assert retried.data["agent_id"] == first.data["agent_id"]
    assert retried.data["launch_attempt_id"] == retry_attempt
    assert "agent_recovery_authority" not in retried.data
    assert activated.data["generation"] == 1
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(LogicalAgentPrincipal)
            )
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(WindowIdentity))
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeBinding))
            == 1
        )


@pytest.mark.asyncio
async def test_tool_error_transport_contains_versioned_sanitized_envelope(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = "test-fleet-controller-owner"
    configure_database_profile(monkeypatch, owner_token=owner_token)

    async with Client(build_mcp_server()) as client:
        with pytest.raises(ToolError) as failed:
            await client.call_tool(
                "publish_fleet_launch_state",
                {
                    "project_key": "/regression/error-transport",
                    "logical_agent_key": f"fa:{uuid.uuid4()}",
                    "desired_state": "running",
                    "coordination_state": "pending",
                    "supervisor_sequence": 1,
                    "launch_attempt_id": str(uuid.uuid4()),
                    "identity_context_injected": False,
                    "owner_token": "wrong-secret-owner-token",
                },
            )

    envelope = decode_error_marker(str(failed.value))
    assert envelope["type"] == "OWNER_AUTHENTICATION_REQUIRED"
    assert envelope["operation"] == "publish_fleet_launch_state"
    assert envelope["retry_class"] == "operator_action"
    serialized = str(envelope)
    assert "wrong-secret-owner-token" not in serialized
