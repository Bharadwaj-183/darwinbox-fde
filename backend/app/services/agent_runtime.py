from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from app.agent.orchestrator import run_migration_agent

_tasks: dict[str, asyncio.Task[Any]] = {}


def start_agent(migration_id: str) -> asyncio.Task[Any]:
    existing = _tasks.get(migration_id)
    if existing is not None and not existing.done():
        return existing

    task = asyncio.create_task(run_migration_agent(migration_id), name=f"migration-agent:{migration_id}")
    _tasks[migration_id] = task

    def _cleanup(done: asyncio.Task[Any]) -> None:
        current = _tasks.get(migration_id)
        if current is done:
            _tasks.pop(migration_id, None)

    task.add_done_callback(_cleanup)
    return task


async def stop_agent(migration_id: str) -> bool:
    task = _tasks.get(migration_id)
    if task is None or task.done():
        _tasks.pop(migration_id, None)
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # The agent itself records failures; stopping should remain successful.
        pass
    finally:
        _tasks.pop(migration_id, None)
    return True


def active_agent_ids() -> set[str]:
    return {migration_id for migration_id, task in _tasks.items() if not task.done()}
