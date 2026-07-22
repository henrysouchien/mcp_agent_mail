"""Best-effort local notification signals without the legacy Git boundary."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings

_SIGNAL_DEBOUNCE: dict[tuple[str, str], float] = {}


def _signal_path(settings: Settings, project_slug: str, agent_name: str) -> Path:
    root = Path(settings.notifications.signals_dir).expanduser().resolve()
    return root / "projects" / project_slug / "agents" / f"{agent_name}.signal"


async def emit_notification_signal(
    settings: Settings,
    project_slug: str,
    agent_name: str,
    message_metadata: dict[str, Any] | None = None,
) -> bool:
    """Atomically update an agent's best-effort local notification signal."""
    if not settings.notifications.enabled:
        return False
    debounce_key = (project_slug, agent_name)
    now_ms = time.time() * 1000
    if now_ms - _SIGNAL_DEBOUNCE.get(debounce_key, 0) < settings.notifications.debounce_ms:
        return False
    _SIGNAL_DEBOUNCE[debounce_key] = now_ms
    signal_path = _signal_path(settings, project_slug, agent_name)
    signal_data: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project": project_slug,
        "agent": agent_name,
    }
    if settings.notifications.include_metadata and message_metadata:
        signal_data["message"] = {
            "id": message_metadata.get("id"),
            "from": message_metadata.get("from"),
            "subject": message_metadata.get("subject"),
            "importance": message_metadata.get("importance", "normal"),
        }

    def write_signal() -> None:
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = signal_path.with_suffix(f"{signal_path.suffix}.tmp")
        temporary_path.write_text(json.dumps(signal_data, indent=2), encoding="utf-8")
        temporary_path.replace(signal_path)

    try:
        await asyncio.to_thread(write_signal)
        return True
    except Exception:
        return False


async def clear_notification_signal(
    settings: Settings,
    project_slug: str,
    agent_name: str,
) -> bool:
    """Remove an agent's local notification signal after inbox synchronization."""
    if not settings.notifications.enabled:
        return False
    signal_path = _signal_path(settings, project_slug, agent_name)

    def clear_signal() -> bool:
        if not signal_path.exists():
            return False
        signal_path.unlink()
        return True

    try:
        return await asyncio.to_thread(clear_signal)
    except Exception:
        return False


def list_pending_signals(
    settings: Settings,
    project_slug: str | None = None,
) -> list[dict[str, Any]]:
    """List parseable local notification signals, optionally by project."""
    if not settings.notifications.enabled:
        return []
    projects_dir = (
        Path(settings.notifications.signals_dir).expanduser().resolve() / "projects"
    )
    if not projects_dir.exists():
        return []
    project_dirs = (
        [projects_dir / project_slug]
        if project_slug
        else list(projects_dir.iterdir())
    )
    results: list[dict[str, Any]] = []
    for project_dir in project_dirs:
        agents_dir = project_dir / "agents"
        if not agents_dir.is_dir():
            continue
        for signal_file in agents_dir.glob("*.signal"):
            try:
                parsed = json.loads(signal_file.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    results.append(parsed)
            except Exception:
                results.append(
                    {
                        "project": project_dir.name,
                        "agent": signal_file.stem,
                        "error": "Failed to parse signal file",
                    }
                )
    return results
