from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mcp_agent_mail.credentials import (
    CredentialGenerationConflictError,
    InvalidCredentialError,
    MalformedCredentialError,
    PepperUnavailableError,
    consume_bootstrap_credential,
    create_bootstrap_credential,
    create_pane_credential,
    revoke_pane_credential,
    rotate_pane_credential,
    verify_bootstrap_credential,
    verify_pane_credential,
)
from mcp_agent_mail.db import ensure_schema, get_immediate_session
from mcp_agent_mail.models import Agent, Project

PEPPERS = {"current": b"p" * 32, "next": b"n" * 32}


def _future() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)


async def _identity() -> tuple[int, int]:
    async with get_immediate_session() as session:
        project = Project(slug="credentials", human_key="/credentials")
        session.add(project)
        await session.flush()
        assert project.id is not None
        agent = Agent(
            project_id=project.id,
            name="BoundAgent",
            program="test",
            model="test",
        )
        session.add(agent)
        await session.flush()
        assert agent.id is not None
        await session.commit()
        return project.id, agent.id


@pytest.mark.asyncio
async def test_pane_credential_persists_binding_and_verifies(isolated_env: object) -> None:
    await ensure_schema()
    project_id, agent_id = await _identity()
    async with get_immediate_session() as session:
        issued = await create_pane_credential(
            session,
            project_id=project_id,
            agent_id=agent_id,
            window_uuid="window-1",
            pepper_key_id="current",
            peppers=PEPPERS,
        )
        await session.commit()

    async with get_immediate_session() as session:
        verified = await verify_pane_credential(session, issued.bearer, peppers=PEPPERS)
        await session.commit()
    assert verified.id == issued.record.id
    assert verified.project_id == project_id
    assert verified.agent_id == agent_id
    assert verified.window_uuid == "window-1"


@pytest.mark.asyncio
async def test_pane_rotation_uses_generation_cas_and_revokes_old_bearer(
    isolated_env: object,
) -> None:
    await ensure_schema()
    project_id, agent_id = await _identity()
    async with get_immediate_session() as session:
        issued = await create_pane_credential(
            session,
            project_id=project_id,
            agent_id=agent_id,
            window_uuid="window-rotate",
            pepper_key_id="current",
            peppers=PEPPERS,
        )
        await session.commit()

    async with get_immediate_session() as session:
        rotated = await rotate_pane_credential(
            session,
            issued.record.id,
            expected_generation=1,
            pepper_key_id="next",
            peppers=PEPPERS,
        )
        await session.commit()
    async with get_immediate_session() as session:
        with pytest.raises(InvalidCredentialError):
            await verify_pane_credential(session, issued.bearer, peppers=PEPPERS)
        verified = await verify_pane_credential(session, rotated.bearer, peppers=PEPPERS)
        assert verified.generation == 2
        with pytest.raises(CredentialGenerationConflictError):
            await rotate_pane_credential(
                session,
                issued.record.id,
                expected_generation=1,
                pepper_key_id="next",
                peppers=PEPPERS,
            )


@pytest.mark.asyncio
async def test_pane_credential_fails_closed_for_missing_pepper_and_revocation(
    isolated_env: object,
) -> None:
    await ensure_schema()
    project_id, agent_id = await _identity()
    async with get_immediate_session() as session:
        issued = await create_pane_credential(
            session,
            project_id=project_id,
            agent_id=agent_id,
            window_uuid="window-revoke",
            pepper_key_id="current",
            peppers=PEPPERS,
        )
        await session.commit()
    async with get_immediate_session() as session:
        with pytest.raises(PepperUnavailableError):
            await verify_pane_credential(session, issued.bearer, peppers={})
        await revoke_pane_credential(session, issued.record.id, reason="retired")
        await session.commit()
    async with get_immediate_session() as session:
        with pytest.raises(InvalidCredentialError):
            await verify_pane_credential(session, issued.bearer, peppers=PEPPERS)


@pytest.mark.asyncio
async def test_malformed_or_tampered_pane_bearers_are_rejected(isolated_env: object) -> None:
    await ensure_schema()
    async with get_immediate_session() as session:
        with pytest.raises(MalformedCredentialError):
            await verify_pane_credential(session, "not-a-bearer", peppers=PEPPERS)
        with pytest.raises(MalformedCredentialError):
            await verify_pane_credential(session, "id.1.short", peppers=PEPPERS)


@pytest.mark.asyncio
async def test_bootstrap_is_window_bound_and_single_use(isolated_env: object) -> None:
    await ensure_schema()
    project_id, agent_id = await _identity()
    async with get_immediate_session() as session:
        issued = await create_bootstrap_credential(
            session,
            project_id=project_id,
            prospective_project_digest=None,
            window_uuid="window-bootstrap",
            pepper_key_id="current",
            peppers=PEPPERS,
            expires_ts=_future(),
        )
        await session.commit()

    async with get_immediate_session() as session:
        with pytest.raises(InvalidCredentialError):
            await verify_bootstrap_credential(
                session,
                issued.bearer,
                peppers=PEPPERS,
                window_uuid="wrong-window",
            )
        record = await verify_bootstrap_credential(
            session,
            issued.bearer,
            peppers=PEPPERS,
            window_uuid="window-bootstrap",
        )
        await consume_bootstrap_credential(
            session,
            record,
            agent_id=agent_id,
            idempotency_key="registration-key",
        )
        record.expires_ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        await session.commit()

    async with get_immediate_session() as session:
        with pytest.raises(InvalidCredentialError):
            await verify_bootstrap_credential(
                session,
                issued.bearer,
                peppers=PEPPERS,
                window_uuid="window-bootstrap",
            )
        replay_record = await verify_bootstrap_credential(
            session,
            issued.bearer,
            peppers=PEPPERS,
            window_uuid="window-bootstrap",
            allow_consumed_idempotency_key="registration-key",
        )
        assert replay_record.consumed_idempotency_key == "registration-key"


@pytest.mark.asyncio
async def test_bootstrap_consumption_rolls_back_atomically(isolated_env: object) -> None:
    await ensure_schema()
    project_id, agent_id = await _identity()
    async with get_immediate_session() as session:
        issued = await create_bootstrap_credential(
            session,
            project_id=project_id,
            prospective_project_digest=None,
            window_uuid="window-rollback",
            pepper_key_id="current",
            peppers=PEPPERS,
            expires_ts=_future(),
        )
        await session.commit()
    async with get_immediate_session() as session:
        record = await verify_bootstrap_credential(
            session,
            issued.bearer,
            peppers=PEPPERS,
            window_uuid="window-rollback",
        )
        await consume_bootstrap_credential(
            session,
            record,
            agent_id=agent_id,
            idempotency_key="rollback-key",
        )
        await session.rollback()
    async with get_immediate_session() as session:
        verified = await verify_bootstrap_credential(
            session,
            issued.bearer,
            peppers=PEPPERS,
            window_uuid="window-rollback",
        )
        assert verified.consumed_ts is None
