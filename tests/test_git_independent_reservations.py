from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from fastmcp import Client
from sqlalchemy import func, select

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.audit import append_audit_event, verify_audit_chain
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session
from mcp_agent_mail.models import (
    Agent,
    AuditEvent,
    FileReservation,
    IdempotencyRecord,
    Project,
    ProjectStorageCutover,
)


async def _create_project_and_agents() -> int:
    async with get_immediate_session() as session:
        project = Project(slug="reservation-core", human_key="/reservation-core")
        session.add(project)
        await session.flush()
        assert project.id is not None
        session.add_all(
            [
                Agent(
                    project_id=project.id,
                    name="FirstAgent",
                    program="test",
                    model="test",
                    registration_token="first-secret",
                ),
                Agent(
                    project_id=project.id,
                    name="SecondAgent",
                    program="test",
                    model="test",
                    registration_token="second-secret",
                ),
            ]
        )
        baseline = await append_audit_event(
            session,
            project_id=project.id,
            actor_kind="system",
            actor_scope_id="system/new-project",
            actor_agent_id=None,
            operation_kind="project_created_v1",
            entity_type="project",
            entity_id=str(project.id),
            payload_version="project-created-v1",
            payload={"slug": project.slug},
        )
        await session.flush()
        assert baseline.id is not None
        session.add(
            ProjectStorageCutover(
                project_id=project.id,
                state="git_independent",
                generation=1,
                baseline_event_id=baseline.id,
            )
        )
        await session.commit()
        return project.id


@pytest.mark.asyncio
async def test_core_reservation_retries_replay_one_atomic_receipt(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "migration")
    clear_settings_cache()
    await ensure_schema()
    project_id = await _create_project_and_agents()

    async def legacy_path_must_not_run(*args, **kwargs):
        raise AssertionError("Git-independent reservation opened a legacy archive")

    monkeypatch.setattr("mcp_agent_mail.app.ensure_archive", legacy_path_must_not_run)
    monkeypatch.setattr("mcp_agent_mail.app._git_context_metadata", legacy_path_must_not_run)
    arguments = {
        "project_key": "/reservation-core",
        "agent_name": "FirstAgent",
        "registration_token": "first-secret",
        "paths": ["src/core.py"],
        "ttl_seconds": 600,
        "exclusive": True,
        "reason": "atomic test",
        "idempotency_key": "reserve-once",
    }
    async with Client(build_mcp_server()) as client:
        first = await client.call_tool("file_reservation_paths", arguments)
        replay = await client.call_tool("file_reservation_paths", arguments)
        renew_arguments = {
            "project_key": "/reservation-core",
            "agent_name": "FirstAgent",
            "registration_token": "first-secret",
            "paths": ["src/core.py"],
            "extend_seconds": 900,
            "idempotency_key": "renew-once",
        }
        renewed = await client.call_tool("renew_file_reservations", renew_arguments)
        renewed_replay = await client.call_tool(
            "renew_file_reservations",
            renew_arguments,
        )
        release_arguments = {
            "project_key": "/reservation-core",
            "agent_name": "FirstAgent",
            "registration_token": "first-secret",
            "paths": ["src/core.py"],
            "idempotency_key": "release-once",
        }
        released = await client.call_tool(
            "release_file_reservations",
            release_arguments,
        )
        released_replay = await client.call_tool(
            "release_file_reservations",
            release_arguments,
        )

    assert first.data["replayed"] is False
    assert replay.data["replayed"] is True
    assert replay.data["granted"] == first.data["granted"]
    assert replay.data["event_hash"] == first.data["event_hash"]
    assert replay.data["retry_safety"] == "safe_with_idempotency_key"
    assert renewed.data["replayed"] is False
    assert renewed_replay.data["replayed"] is True
    assert renewed_replay.data["file_reservations"] == renewed.data["file_reservations"]
    assert released.data["released"] == 1
    assert released.data["replayed"] is False
    assert released_replay.data["released"] == 1
    assert released_replay.data["replayed"] is True
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(FileReservation)) == 1
        reservation = (await session.execute(select(FileReservation))).scalar_one()
        assert reservation.released_ts is not None
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 3
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 4
        chain = await verify_audit_chain(session, project_id)
    assert chain.valid is True


@pytest.mark.asyncio
async def test_core_reservation_conflict_is_audited_without_git(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "migration")
    clear_settings_cache()
    await ensure_schema()
    project_id = await _create_project_and_agents()
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/reservation-core",
                "agent_name": "FirstAgent",
                "registration_token": "first-secret",
                "paths": ["src/**"],
                "idempotency_key": "first-reservation",
            },
        )
        second = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/reservation-core",
                "agent_name": "SecondAgent",
                "registration_token": "second-secret",
                "paths": ["src/core.py"],
                "idempotency_key": "second-reservation",
            },
        )

    assert len(second.data["granted"]) == 1
    assert second.data["conflicts"][0]["path"] == "src/core.py"
    assert second.data["conflicts"][0]["holders"][0]["agent"] == "FirstAgent"
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(FileReservation)) == 2
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 2
        chain = await verify_audit_chain(session, project_id)
    assert chain.valid is True


@pytest.mark.asyncio
async def test_core_reservation_resource_expires_with_atomic_audit_and_no_archive(
    isolated_env: object,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNTIME_PROFILE", "migration")
    clear_settings_cache()
    await ensure_schema()
    project_id = await _create_project_and_agents()
    async with get_immediate_session() as session:
        agent = (
            await session.execute(
                select(Agent).where(cast(Any, Agent.name) == "FirstAgent")
            )
        ).scalar_one()
        session.add(
            FileReservation(
                project_id=project_id,
                agent_id=agent.id,
                path_pattern="expired.txt",
                exclusive=True,
                expires_ts=(datetime.now(timezone.utc) - timedelta(minutes=1)).replace(
                    tzinfo=None
                ),
            )
        )
        await session.commit()

    async def legacy_path_must_not_run(*args, **kwargs):
        raise AssertionError("Core expiry attempted to write a legacy archive")

    monkeypatch.setattr(
        "mcp_agent_mail.app._write_file_reservation_records",
        legacy_path_must_not_run,
    )
    async with Client(build_mcp_server()) as client:
        await client.read_resource(
            "resource://file_reservations/reservation-core?active_only=false"
        )

    async with get_session() as session:
        reservation = (await session.execute(select(FileReservation))).scalar_one()
        assert reservation.released_ts is not None
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 2
        chain = await verify_audit_chain(session, project_id)
    assert chain.valid is True
