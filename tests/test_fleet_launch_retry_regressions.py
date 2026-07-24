from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.error_contract import decode_error_marker
from mcp_agent_mail.models import (
    ActivePrincipalAuthority,
    Agent,
    FleetLaunchState,
    LogicalAgentPrincipal,
    Project,
    RuntimeBinding,
    RuntimeObservation,
    WindowIdentity,
)


def configure_database_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    owner_token = "test-fleet-controller-owner"
    monkeypatch.setenv("RUNTIME_PROFILE", "database")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    monkeypatch.setenv("CORE_OWNER_TOKEN", owner_token)
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "")
    _config.clear_settings_cache()
    return owner_token


def ensure_arguments(
    *,
    project: str,
    logical_key: str,
    attempt_id: str,
    locator: str,
    owner_token: str,
    supervisor_sequence: int,
    proof_mode: str = "create",
    expected_agent_id: int | None = None,
    recovery_authority: str | None = None,
    recovery_attempt_id: str | None = None,
    task_description: str = "same-workload retry regression",
) -> dict[str, Any]:
    project_hash = hashlib.sha256(project.encode()).hexdigest()
    arguments = {
        "canonical_project": project,
        "logical_agent_key": logical_key,
        "launch_attempt_id": attempt_id,
        "proof_mode": proof_mode,
        "window_locator": locator,
        "provider": "claude",
        "requested_model": "fable",
        "task_description": task_description,
        "owner_token": owner_token,
        "idempotency_key": (
            f"ensure-principal:v1:{project_hash}:{logical_key}:{attempt_id}"
        ),
        "supervisor_sequence": supervisor_sequence,
        "expected_generation": 0,
    }
    if expected_agent_id is not None:
        arguments["expected_agent_id"] = expected_agent_id
    if recovery_authority is not None:
        arguments["agent_recovery_authority"] = recovery_authority
    if recovery_attempt_id is not None:
        arguments["recovery_launch_attempt_id"] = recovery_attempt_id
    return arguments


async def authority_counts() -> dict[str, int]:
    models = {
        "projects": Project,
        "agents": Agent,
        "assignments": LogicalAgentPrincipal,
        "identities": WindowIdentity,
        "authorities": ActivePrincipalAuthority,
        "launches": FleetLaunchState,
        "runtimes": RuntimeBinding,
        "observations": RuntimeObservation,
    }
    async with get_session() as session:
        return {
            name: int(
                await session.scalar(select(func.count()).select_from(model))
                or 0
            )
            for name, model in models.items()
        }


@pytest.mark.asyncio
async def test_locator_conflict_is_typed_and_creates_no_ghost_rows(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    owner_token = configure_database_profile(monkeypatch)
    project = "/regression/fleet-locator-conflict"
    locator = str(uuid.uuid4())
    first = ensure_arguments(
        project=project,
        logical_key=f"fa:{uuid.uuid4()}",
        attempt_id=str(uuid.uuid4()),
        locator=locator,
        owner_token=owner_token,
        supervisor_sequence=1,
    )
    second = ensure_arguments(
        project=project,
        logical_key=f"fa:{uuid.uuid4()}",
        attempt_id=str(uuid.uuid4()),
        locator=locator,
        owner_token=owner_token,
        supervisor_sequence=1,
    )

    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_fleet_principal", first)
        before = await authority_counts()
        with pytest.raises(ToolError) as error:
            await client.call_tool("ensure_fleet_principal", second)
        envelope = decode_error_marker(str(error.value))
        after = await authority_counts()

    assert envelope["type"] == "WINDOW_LOCATOR_ALREADY_ASSIGNED"
    assert envelope["retry_class"] == "operator_action"
    assert before == after
    assert after == {
        "projects": 1,
        "agents": 1,
        "assignments": 1,
        "identities": 1,
        "authorities": 1,
        "launches": 1,
        "runtimes": 0,
        "observations": 0,
    }


@pytest.mark.asyncio
async def test_failed_create_retries_same_principal_with_new_attempt(
    isolated_env: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_env
    owner_token = configure_database_profile(monkeypatch)
    project = "/regression/fleet-failed-create-retry"
    logical_key = f"fa:{uuid.uuid4()}"
    locator = str(uuid.uuid4())
    first_attempt = str(uuid.uuid4())
    retry_attempt = str(uuid.uuid4())
    first_args = ensure_arguments(
        project=project,
        logical_key=logical_key,
        attempt_id=first_attempt,
        locator=locator,
        owner_token=owner_token,
        supervisor_sequence=1,
    )

    async with Client(build_mcp_server()) as client:
        first = await client.call_tool("ensure_fleet_principal", first_args)
        failed = await client.call_tool(
            "publish_fleet_launch_state",
            {
                "project_key": project,
                "logical_agent_key": logical_key,
                "desired_state": "running",
                "coordination_state": "failed",
                "supervisor_sequence": 2,
                "launch_attempt_id": first_attempt,
                "identity_context_injected": False,
                "owner_token": owner_token,
            },
        )
        assert failed.data["coordination_state"] == "failed"

        retry = await client.call_tool(
            "ensure_fleet_principal",
            ensure_arguments(
                project=project,
                logical_key=logical_key,
                attempt_id=retry_attempt,
                locator=locator,
                owner_token=owner_token,
                supervisor_sequence=3,
                proof_mode="retry_failed_create",
                expected_agent_id=first.data["agent_id"],
                recovery_authority=first.data[
                    "agent_recovery_authority"
                ],
                recovery_attempt_id=first_attempt,
                task_description="same pane, replacement provider session",
            ),
        )

    assert retry.data["agent_id"] == first.data["agent_id"]
    assert retry.data["agent_name"] == first.data["agent_name"]
    assert retry.data["overall"] == "starting"
    assert await authority_counts() == {
        "projects": 1,
        "agents": 1,
        "assignments": 1,
        "identities": 1,
        "authorities": 1,
        "launches": 1,
        "runtimes": 0,
        "observations": 0,
    }
