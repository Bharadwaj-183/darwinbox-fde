import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator


class EventBus:
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)

    async def publish(self, migration_id: str, event: dict) -> None:
        for queue in list(self._queues[migration_id]):
            await queue.put(event)

    async def stream(self, migration_id: str) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._queues[migration_id].add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield event
                except asyncio.TimeoutError:
                    yield {"type": "keepalive", "migration_id": migration_id}
        finally:
            self._queues[migration_id].discard(queue)
            if not self._queues[migration_id]:
                self._queues.pop(migration_id, None)


event_bus = EventBus()
