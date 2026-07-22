"""Lazy boundary around the optional legacy Git/archive implementation."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .config import get_settings

ProjectArchive = Any


class LegacyStorageUnavailableError(RuntimeError):
    """A core process attempted to cross the optional legacy boundary."""


def _implementation(name: str) -> Any:
    if get_settings().runtime_profile == "core":
        raise LegacyStorageUnavailableError(
            f"legacy Git/archive operation {name!r} is unavailable in core runtime"
        )
    module = importlib.import_module(".storage", __package__)
    return getattr(module, name)


def create_project_archive(*args: Any, **kwargs: Any) -> Any:
    return _implementation("ProjectArchive")(*args, **kwargs)


def clear_repo_cache() -> int:
    return _implementation("clear_repo_cache")()


def collect_lock_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _implementation("collect_lock_status")(*args, **kwargs)


def get_fd_headroom() -> int:
    return _implementation("get_fd_headroom")()


def get_fd_usage() -> tuple[int, int]:
    return _implementation("get_fd_usage")()


def get_lock_telemetry() -> dict[str, int]:
    return _implementation("get_lock_telemetry")()


def get_repo_cache_stats() -> dict[str, int]:
    return _implementation("get_repo_cache_stats")()


def proactive_fd_cleanup(*, threshold: int = 100) -> int:
    return _implementation("proactive_fd_cleanup")(threshold=threshold)


@asynccontextmanager
async def archive_write_lock(
    archive: Any,
    *,
    timeout_seconds: float = 60.0,
) -> AsyncIterator[None]:
    async with _implementation("archive_write_lock")(
        archive,
        timeout_seconds=timeout_seconds,
    ):
        yield


async def ensure_archive(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("ensure_archive")(*args, **kwargs)


async def heal_archive_locks(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _implementation("heal_archive_locks")(*args, **kwargs)


async def process_attachments(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("process_attachments")(*args, **kwargs)


async def write_agent_profile(*args: Any, **kwargs: Any) -> None:
    await _implementation("write_agent_profile")(*args, **kwargs)


async def write_file_reservation_records(*args: Any, **kwargs: Any) -> None:
    await _implementation("write_file_reservation_records")(*args, **kwargs)


async def write_message_bundle(*args: Any, **kwargs: Any) -> None:
    await _implementation("write_message_bundle")(*args, **kwargs)


async def emit_notification_signal(*args: Any, **kwargs: Any) -> None:
    await _implementation("emit_notification_signal")(*args, **kwargs)


async def clear_notification_signal(*args: Any, **kwargs: Any) -> None:
    await _implementation("clear_notification_signal")(*args, **kwargs)


async def write_file_reservation_record(*args: Any, **kwargs: Any) -> None:
    await _implementation("write_file_reservation_record")(*args, **kwargs)


async def get_agent_communication_graph(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("get_agent_communication_graph")(*args, **kwargs)


async def get_archive_tree(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("get_archive_tree")(*args, **kwargs)


async def get_commit_detail(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("get_commit_detail")(*args, **kwargs)


async def get_file_content(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("get_file_content")(*args, **kwargs)


async def get_historical_inbox_snapshot(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("get_historical_inbox_snapshot")(*args, **kwargs)


async def get_message_commit_sha(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("get_message_commit_sha")(*args, **kwargs)


async def get_recent_commits(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("get_recent_commits")(*args, **kwargs)


async def get_timeline_commits(*args: Any, **kwargs: Any) -> Any:
    return await _implementation("get_timeline_commits")(*args, **kwargs)
