"""Lazy boundary around the optional legacy Git/archive implementation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

ProjectArchive = Any


def create_project_archive(*args: Any, **kwargs: Any) -> Any:
    from .storage import ProjectArchive as implementation

    return implementation(*args, **kwargs)


def clear_repo_cache() -> int:
    from .storage import clear_repo_cache as implementation

    return implementation()


def collect_lock_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .storage import collect_lock_status as implementation

    return implementation(*args, **kwargs)


def get_fd_headroom() -> int:
    from .storage import get_fd_headroom as implementation

    return implementation()


def get_fd_usage() -> tuple[int, int]:
    from .storage import get_fd_usage as implementation

    return implementation()


def get_lock_telemetry() -> dict[str, int]:
    from .storage import get_lock_telemetry as implementation

    return implementation()


def get_repo_cache_stats() -> dict[str, int]:
    from .storage import get_repo_cache_stats as implementation

    return implementation()


def proactive_fd_cleanup(*, threshold: int = 100) -> int:
    from .storage import proactive_fd_cleanup as implementation

    return implementation(threshold=threshold)


@asynccontextmanager
async def archive_write_lock(
    archive: Any,
    *,
    timeout_seconds: float = 60.0,
) -> AsyncIterator[None]:
    from .storage import archive_write_lock as implementation

    async with implementation(archive, timeout_seconds=timeout_seconds):
        yield


async def ensure_archive(*args: Any, **kwargs: Any) -> Any:
    from .storage import ensure_archive as implementation

    return await implementation(*args, **kwargs)


async def heal_archive_locks(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .storage import heal_archive_locks as implementation

    return await implementation(*args, **kwargs)


async def process_attachments(*args: Any, **kwargs: Any) -> Any:
    from .storage import process_attachments as implementation

    return await implementation(*args, **kwargs)


async def write_agent_profile(*args: Any, **kwargs: Any) -> None:
    from .storage import write_agent_profile as implementation

    await implementation(*args, **kwargs)


async def write_file_reservation_records(*args: Any, **kwargs: Any) -> None:
    from .storage import write_file_reservation_records as implementation

    await implementation(*args, **kwargs)


async def write_message_bundle(*args: Any, **kwargs: Any) -> None:
    from .storage import write_message_bundle as implementation

    await implementation(*args, **kwargs)


async def emit_notification_signal(*args: Any, **kwargs: Any) -> None:
    from .storage import emit_notification_signal as implementation

    await implementation(*args, **kwargs)


async def clear_notification_signal(*args: Any, **kwargs: Any) -> None:
    from .storage import clear_notification_signal as implementation

    await implementation(*args, **kwargs)


async def write_file_reservation_record(*args: Any, **kwargs: Any) -> None:
    from .storage import write_file_reservation_record as implementation

    await implementation(*args, **kwargs)


async def get_agent_communication_graph(*args: Any, **kwargs: Any) -> Any:
    from .storage import get_agent_communication_graph as implementation

    return await implementation(*args, **kwargs)


async def get_archive_tree(*args: Any, **kwargs: Any) -> Any:
    from .storage import get_archive_tree as implementation

    return await implementation(*args, **kwargs)


async def get_commit_detail(*args: Any, **kwargs: Any) -> Any:
    from .storage import get_commit_detail as implementation

    return await implementation(*args, **kwargs)


async def get_file_content(*args: Any, **kwargs: Any) -> Any:
    from .storage import get_file_content as implementation

    return await implementation(*args, **kwargs)


async def get_historical_inbox_snapshot(*args: Any, **kwargs: Any) -> Any:
    from .storage import get_historical_inbox_snapshot as implementation

    return await implementation(*args, **kwargs)


async def get_message_commit_sha(*args: Any, **kwargs: Any) -> Any:
    from .storage import get_message_commit_sha as implementation

    return await implementation(*args, **kwargs)


async def get_recent_commits(*args: Any, **kwargs: Any) -> Any:
    from .storage import get_recent_commits as implementation

    return await implementation(*args, **kwargs)


async def get_timeline_commits(*args: Any, **kwargs: Any) -> Any:
    from .storage import get_timeline_commits as implementation

    return await implementation(*args, **kwargs)
