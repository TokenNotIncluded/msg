"""One bounded public snapshot; every rendered reference still needs fresh ACLs."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from msg.core.codec import canonical
from msg.core.errors import require
from msg.storage.session import RelationalSession

log = logging.getLogger(__name__)
_snapshot = contextvars.ContextVar('msg_home_snapshot', default=None)
_versions = contextvars.ContextVar('msg_home_versions', default=None)


@dataclass
class HomeSnapshot:
    data: dict | None = None
    board: dict | None = None
    versions: dict = field(default_factory=dict)
    created: float = 0
    invalidate: object = None


def current_snapshot():
    return _snapshot.get()


def not_modified(request, etag):
    return any(
        tag.strip().removeprefix('W/') in {'*', etag}
        for tag in request.headers.get('if-none-match', '').split(',')
    )


def record_version(path, resource):
    versions = _versions.get()
    if versions is not None:
        versions[path] = (resource.id, resource.generation, resource.revision)


@contextmanager
def render_snapshot(snapshot):
    token = _snapshot.set(snapshot)
    try:
        yield
    finally:
        _snapshot.reset(token)


class PublicHomeCache:
    """A single service key, single flight, bounded stale age and cold wait.

    The loader must execute anonymous operations, never capture a Request,
    browser credential, transaction or account projection. No HTML is stored.
    """

    def __init__(
        self, loader, *, ttl=5, stale=60, cold_wait=0.25, max_bytes=262144, clock=time.monotonic
    ):
        self.loader, self.ttl, self.stale = loader, ttl, stale
        self.cold_wait, self.clock = cold_wait, clock
        self.max_bytes = max_bytes
        self.snapshot = None
        self.board = None
        self.pending = None
        self.retry_at = 0
        self.closed = False

    def invalidate(self):
        if self.snapshot is not None:
            self.snapshot.created = min(self.snapshot.created, self.clock() - self.ttl)
        self.retry_at = 0

    async def _refresh(self):
        versions = {}
        token = _versions.set(versions)
        try:
            data, board = await self.loader()
            if data is None:
                # A failed scan must not replace a usable snapshot with zeros.
                # The public board is one setting and stays readable on its own.
                if board is not None and len(canonical({'board': board})) <= self.max_bytes:
                    self.board = board
                self.retry_at = self.clock() + self.ttl
                return
            chosen = board if board is not None else self.board
            require(
                len(canonical({'data': data, 'board': chosen})) <= self.max_bytes,
                'response_too_large',
            )
            if board is not None:
                self.board = board
            self.snapshot = HomeSnapshot(data, chosen, versions, self.clock(), self.invalidate)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.retry_at = self.clock() + self.ttl
            log.error('Public homepage snapshot refresh failed')
        finally:
            _versions.reset(token)

    async def get(self):
        now = self.clock()
        if self.closed:
            return HomeSnapshot()
        usable = self.snapshot is not None and now - self.snapshot.created <= self.stale
        fresh = usable and now - self.snapshot.created < self.ttl
        if not fresh and now >= self.retry_at and (self.pending is None or self.pending.done()):
            self.pending = asyncio.create_task(self._refresh(), name='msg-public-home-refresh')
        if not usable and self.pending is not None and not self.pending.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.pending), self.cold_wait)
            except TimeoutError:
                pass
        if self.snapshot is None or self.clock() - self.snapshot.created > self.stale:
            return HomeSnapshot(board=self.board)
        return self.snapshot

    async def close(self):
        self.closed = True
        if self.pending is not None and not self.pending.done():
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
        self.snapshot = None


class HomeReadSession(RelationalSession):
    """Deduplicate repeated reads within one homepage operation only.

    The underlying transaction and task still own all access. This memo never
    survives a request and is deliberately unavailable to write transactions.
    It stores database facts, not authorization outcomes.
    """

    def __init__(self, session, *, limit=4096):
        require(not session.write, 'read_only_transaction')
        super().__init__(write=False)
        self.session, self.limit = session, limit
        self.memo = {}
        self.resources = {}

    def check(self, write=False):
        self.session.check(write)
        require(not write, 'read_only_transaction')

    def execute(self, sql, parameters=(), *, write=False):
        self.check(write)
        return self.session.execute(sql, parameters, write=write)

    def _read(self, sql, parameters, many):
        self.check()
        # These are deliberately live fences even inside a read operation.
        # A restore/quarantine transition must never consult memoized authority.
        if tuple(parameters) in {
            ('recovery_runtime_generation',),
            ('recovery_quarantine',),
        }:
            return self.session.rows(sql, parameters) if many else self.session.one(sql, parameters)
        key = (many, sql, tuple(parameters))
        if key in self.memo:
            value = self.memo[key]
            return list(value) if many else value
        value = self.session.rows(sql, parameters) if many else self.session.one(sql, parameters)
        if len(self.memo) < self.limit and (not many or len(value) <= 128):
            self.memo[key] = tuple(value) if many else value
        return value

    def one(self, sql, parameters=()):
        return self._read(sql, parameters, False)

    def rows(self, sql, parameters=()):
        return self._read(sql, parameters, True)

    async def resource(self, id):
        self.check()
        if id not in self.resources:
            value = await super().resource(id)
            if len(self.resources) >= self.limit:
                return value
            self.resources[id] = value
        return self.resources[id]
