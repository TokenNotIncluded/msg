"""Ephemeral wake-up hints for durable PostgreSQL effect jobs.

The database is the source of truth. A missed Pub/Sub message must be handled
by the worker's normal database polling, never by replaying Valkey messages.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

import valkey


class ValkeyOutboxSignal:
    def __init__(self, url: str, *, channel: str = "msgd:effects") -> None:
        self.client = valkey.Valkey.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        self.channel = channel

    async def publish_pending(self, effect_ids: Iterable[str]) -> None:
        """Publish job IDs after their PostgreSQL transaction has committed."""
        ids = tuple(effect_ids)
        if ids:
            await asyncio.to_thread(self._publish_pending, ids)

    def _publish_pending(self, effect_ids: tuple[str, ...]) -> None:
        for effect_id in effect_ids:
            self.client.publish(self.channel, effect_id)

    async def wait_for_pending(self, timeout: float = 1.0) -> bool:
        """Wait for a hint; false means the caller should poll PostgreSQL."""
        return await asyncio.to_thread(self._wait_for_pending, timeout)

    def _wait_for_pending(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.client.pubsub(ignore_subscribe_messages=True) as subscriber:
            subscriber.subscribe(self.channel)
            while remaining := deadline - time.monotonic():
                if remaining <= 0:
                    break
                message = subscriber.get_message(timeout=remaining)
                if message and message.get("type") == "message":
                    return True
        return False
