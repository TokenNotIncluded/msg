"""Session-bound query results without a public driver cursor/connection."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol

from msg.core.query import SqlRow


class _Cursor(Protocol):
    @property
    def rowcount(self) -> int: ...
    def fetchone(self) -> SqlRow | None: ...
    def fetchall(self) -> list[SqlRow]: ...


class SessionQueryResult:
    """A narrow view, not a sandbox for trusted in-process Python consumers."""

    __slots__ = ('_cursor', '_check')

    def __init__(self, cursor: _Cursor, check: Callable[[], None]):
        self._cursor = cursor
        self._check = check

    @property
    def rowcount(self) -> int:
        self._check()
        return self._cursor.rowcount

    def fetchone(self) -> SqlRow | None:
        self._check()
        return self._cursor.fetchone()

    def fetchall(self) -> list[SqlRow]:
        self._check()
        return self._cursor.fetchall()

    def __iter__(self) -> Iterator[SqlRow]:
        while (row := self.fetchone()) is not None:
            yield row
