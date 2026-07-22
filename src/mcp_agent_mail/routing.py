"""Fail-closed per-project routing for the storage migration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from .models import ProjectStorageCutover

LEGACY: Final = "legacy"
QUIESCING: Final = "quiescing"
BASELINE_COMMITTED: Final = "baseline_committed"
GIT_INDEPENDENT: Final = "git_independent"
VALID_STATES: Final = frozenset(
    {LEGACY, QUIESCING, BASELINE_COMMITTED, GIT_INDEPENDENT}
)
VALID_PROFILES: Final = frozenset({"legacy", "migration", "core"})


class StorageRoutingError(RuntimeError):
    """A project cannot be safely served by the selected runtime profile."""


class MutationQuiescedError(StorageRoutingError):
    """Mutations are intentionally stopped while a cutover is being verified."""


@dataclass(frozen=True, slots=True)
class StorageRoute:
    state: str
    generation: int
    retry_safety: str
    use_legacy_adapter: bool


async def resolve_storage_route(
    session: AsyncSession,
    *,
    project_id: int,
    runtime_profile: str,
    for_mutation: bool,
) -> StorageRoute:
    """Resolve one route from transactionally read state, failing closed."""
    if runtime_profile not in VALID_PROFILES:
        raise StorageRoutingError(f"unknown runtime profile {runtime_profile!r}")
    cutover = await session.get(ProjectStorageCutover, project_id)
    if cutover is None:
        if runtime_profile == "core":
            raise StorageRoutingError(
                f"project {project_id} has no cutover state and cannot be served by core"
            )
        state = LEGACY
        generation = 0
    else:
        state = cutover.state
        generation = cutover.generation
    if state not in VALID_STATES:
        raise StorageRoutingError(f"project {project_id} has invalid cutover state {state!r}")

    if runtime_profile == "legacy" and state != LEGACY:
        raise StorageRoutingError(
            f"legacy profile cannot serve project {project_id} in state {state!r}"
        )
    if runtime_profile == "core" and state != GIT_INDEPENDENT:
        raise StorageRoutingError(
            f"core profile requires git_independent state; project {project_id} is {state!r}"
        )
    if for_mutation and state in {QUIESCING, BASELINE_COMMITTED}:
        raise MutationQuiescedError(
            f"project {project_id} mutations are stopped during cutover state {state!r}"
        )
    if state == GIT_INDEPENDENT:
        return StorageRoute(
            state=state,
            generation=generation,
            retry_safety="safe_with_idempotency_key",
            use_legacy_adapter=False,
        )
    return StorageRoute(
        state=state,
        generation=generation,
        retry_safety="unsafe_legacy",
        use_legacy_adapter=True,
    )


async def assert_route_generation(
    session: AsyncSession,
    *,
    project_id: int,
    expected_state: str,
    expected_generation: int,
) -> None:
    """Reject a mutation if its route changed before the commit boundary."""
    cutover = await session.get(ProjectStorageCutover, project_id, populate_existing=True)
    actual_state = cutover.state if cutover is not None else LEGACY
    actual_generation = cutover.generation if cutover is not None else 0
    if actual_state != expected_state or actual_generation != expected_generation:
        raise StorageRoutingError(
            f"project {project_id} storage route changed during mutation"
        )
