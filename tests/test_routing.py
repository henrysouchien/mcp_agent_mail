from __future__ import annotations

import pytest

from mcp_agent_mail.audit import append_audit_event
from mcp_agent_mail.config import ConfigError, clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_immediate_session
from mcp_agent_mail.models import Project, ProjectStorageCutover
from mcp_agent_mail.routing import (
    GIT_INDEPENDENT,
    MutationQuiescedError,
    StorageRoutingError,
    assert_route_generation,
    resolve_storage_route,
)


async def _project() -> int:
    async with get_immediate_session() as session:
        project = Project(slug="routing", human_key="/routing")
        session.add(project)
        await session.flush()
        assert project.id is not None
        await session.commit()
        return project.id


async def _mark_git_independent(project_id: int, *, generation: int) -> None:
    async with get_immediate_session() as session:
        event = await append_audit_event(
            session,
            project_id=project_id,
            actor_kind="system",
            actor_scope_id="system/test-baseline",
            actor_agent_id=None,
            operation_kind="project_created_v1",
            entity_type="project",
            entity_id=str(project_id),
            payload_version="project-created-v1",
            payload={"project_id": project_id},
        )
        await session.flush()
        assert event.id is not None
        session.add(
            ProjectStorageCutover(
                project_id=project_id,
                state=GIT_INDEPENDENT,
                generation=generation,
                baseline_event_id=event.id,
            )
        )
        await session.commit()


def test_runtime_profile_defaults_legacy_and_rejects_unknown(
    isolated_env: object,
    monkeypatch,
) -> None:
    assert get_settings().runtime_profile == "legacy"
    monkeypatch.setenv("RUNTIME_PROFILE", "surprise")
    clear_settings_cache()
    with pytest.raises(ConfigError, match="RUNTIME_PROFILE"):
        get_settings()


@pytest.mark.asyncio
async def test_missing_cutover_is_legacy_only_outside_core(isolated_env: object) -> None:
    await ensure_schema()
    project_id = await _project()
    async with get_immediate_session() as session:
        legacy = await resolve_storage_route(
            session,
            project_id=project_id,
            runtime_profile="migration",
            for_mutation=True,
        )
        assert legacy.state == "legacy"
        assert legacy.generation == 0
        assert legacy.retry_safety == "unsafe_legacy"
        assert legacy.use_legacy_adapter is True
        with pytest.raises(StorageRoutingError):
            await resolve_storage_route(
                session,
                project_id=project_id,
                runtime_profile="core",
                for_mutation=False,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["quiescing", "baseline_committed"])
async def test_cutover_intermediate_states_reject_mutations(
    isolated_env: object,
    state: str,
) -> None:
    await ensure_schema()
    project_id = await _project()
    async with get_immediate_session() as session:
        session.add(ProjectStorageCutover(project_id=project_id, state=state))
        await session.commit()
    async with get_immediate_session() as session:
        with pytest.raises(MutationQuiescedError):
            await resolve_storage_route(
                session,
                project_id=project_id,
                runtime_profile="migration",
                for_mutation=True,
            )


@pytest.mark.asyncio
async def test_git_independent_route_is_safe_and_never_legacy(isolated_env: object) -> None:
    await ensure_schema()
    project_id = await _project()
    await _mark_git_independent(project_id, generation=4)
    async with get_immediate_session() as session:
        route = await resolve_storage_route(
            session,
            project_id=project_id,
            runtime_profile="core",
            for_mutation=True,
        )
        assert route.retry_safety == "safe_with_idempotency_key"
        assert route.use_legacy_adapter is False


@pytest.mark.asyncio
async def test_route_generation_change_fails_closed(isolated_env: object) -> None:
    await ensure_schema()
    project_id = await _project()
    await _mark_git_independent(project_id, generation=2)
    async with get_immediate_session() as session:
        await assert_route_generation(
            session,
            project_id=project_id,
            expected_state=GIT_INDEPENDENT,
            expected_generation=2,
        )
        with pytest.raises(StorageRoutingError):
            await assert_route_generation(
                session,
                project_id=project_id,
                expected_state=GIT_INDEPENDENT,
                expected_generation=1,
            )
