"""One LISTEN connection per process. Notifications carry no event or identity data."""

import asyncio
import contextvars
from contextlib import aclosing, asynccontextmanager, suppress

import psycopg

from msg.storage.postgres import PostgresMetadataStore


class EventSignal:
    def __init__(self, dsn):
        self.dsn = dsn
        self.generation = 0
        self.waiters = 0
        self.ready = asyncio.Event()
        self.changed = asyncio.Event()
        self.task = None
        self.closed = False

    def _bump(self):
        self.generation += 1
        old, self.changed = self.changed, asyncio.Event()
        old.set()

    async def start(self, timeout=2):
        if self.closed:
            return False
        if self.task is None:
            self.task = asyncio.create_task(self._receive(), context=contextvars.Context())
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
        except TimeoutError:
            return False
        return not self.closed

    async def _receive(self):
        delay = 1
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.dsn, autocommit=True, connect_timeout=3
                ) as connection:
                    await connection.execute('LISTEN msg_committed_events')
                    self._bump()
                    self.ready.set()
                    delay = 1
                    async with aclosing(connection.notifies()) as notices:
                        async for _ in notices:
                            self._bump()
            except (psycopg.Error, OSError):
                # The durable log remains authoritative. Reconnection wakes
                # every reader, which repeats its independently authorized read.
                pass
            finally:
                self.ready.clear()
                self._bump()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def wait(self, generation, timeout):
        if self.closed or generation != self.generation:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self.changed.wait(), timeout)

    async def close(self):
        self.closed = True
        self._bump()
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None


class EventMetadataStore(PostgresMetadataStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_signal = EventSignal(self.dsn)

    @asynccontextmanager
    async def transaction(self, *, write):
        async with super().transaction(write=write) as tx:
            yield tx
            if write:
                # PostgreSQL delivers this only after commit. A rollback,
                # including a nested savepoint rollback, cannot wake readers.
                tx.execute("SELECT pg_notify('msg_committed_events', '')", write=True)

    async def close(self):
        await self.event_signal.close()
        await super().close()
