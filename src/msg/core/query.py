"""Explicit SQL query port for trusted server-side domain consumers.

Queries use the shared positional '?' subset; a backend may translate it.
This is not an ORM or an untrusted-SQL interface. Handlers/requirements, local
administrative use cases and workers may consume it within the transaction
provided by MetadataStore; adapters/clients must not construct sessions.
Writes require a write transaction and write=True. Results cannot escape its
owning task or lifetime. Integrity conflicts are constraint_conflict, and
PostgreSQL lock/deadlock timeouts are retryable server_busy; adapter-specific
programming errors still abort the transaction. No commit or connection port
is exposed. Driver and database protections remain the authority for SQL.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

# PostgreSQL SUM(bigint) returns Decimal; ordinary stored times/JSON are text.
type SqlValue = None | bool | int | float | str | bytes | Decimal
type SqlRow = tuple[SqlValue, ...]
type SqlParameters = Sequence[SqlValue]
type SettingValue = (
    None
    | bool
    | int
    | float
    | str
    | list[SettingValue]
    | tuple[SettingValue, ...]
    | Mapping[str, SettingValue]
)


class QueryResult(Protocol):
    @property
    def rowcount(self) -> int: ...
    def fetchone(self) -> SqlRow | None: ...
    def fetchall(self) -> list[SqlRow]: ...
    def __iter__(self) -> Iterator[SqlRow]: ...


@runtime_checkable
class QuerySession(Protocol):
    def execute(
        self, sql: str, parameters: SqlParameters = (), *, write: bool = False
    ) -> QueryResult: ...
    def one(self, sql: str, parameters: SqlParameters = ()) -> SqlRow | None: ...
    def rows(self, sql: str, parameters: SqlParameters = ()) -> list[SqlRow]: ...
    def setting(self, key: str, default: SettingValue = None) -> SettingValue: ...
    def set_setting(self, key: str, value: SettingValue) -> None: ...
