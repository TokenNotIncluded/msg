from pathlib import Path
r=Path('src/msg')
p=r/'storage/sqlite.py'; original=p.read_text()
a=original.index('class SqliteSession:'); b=original.index('\n\nclass SqliteMetadataStore:')
body=original[a:b]
body=body.replace('class SqliteSession:', 'class RelationalSession(ABC):')
body=body.replace('def __init__(self, connection, *, write: bool):\n        self._connection, self.write = connection, write', 'def __init__(self, *, write: bool):\n        self.write = write')
x=body.index('    def execute('); y=body.index('    def one(',x)
body=body[:x]+'''    @abstractmethod
    def execute(self, sql: str, parameters: SqlParameters = (), *,
                write: bool = False) -> QueryResult:
        """Run adapter-normalized SQL in this store-owned transaction."""
        raise NotImplementedError

'''+body[y:]
body=body.replace('def one(self, sql, parameters=()):','def one(self, sql: str, parameters: SqlParameters = ()) -> SqlRow | None:')
body=body.replace('def rows(self, sql, parameters=()):','def rows(self, sql: str, parameters: SqlParameters = ()) -> list[SqlRow]:')
body=body.replace('def setting(self,key,default=None):','def setting(self, key: str, default: SettingValue = None) -> SettingValue:')
body=body.replace('def set_setting(self,key,value):','def set_setting(self, key: str, value: SettingValue) -> None:')
assert '_connection' not in body
models=original[original.index('from msg.core.models import ('):original.index('\n_SCHEMA')]
(r/'storage/session.py').write_text('''"""Shared relational domain methods, with no driver or transaction ownership.

This SQL-oriented monolith shares the supported placeholder/query subset, not
an imaginary database-independent object store. Adapters own SQL translation,
schema migration, connections, writer fences, savepoints and commit/rollback.
Only this session's owning task can access its transaction or results.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import replace
from msg.core.codec import canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.query import QueryResult, SettingValue, SqlParameters, SqlRow
'''+models+'\n\n'+body.rstrip()+'\n')
adapter='''class SqliteSession(RelationalSession):
    def __init__(self, connection, *, write: bool):
        super().__init__(write=write)
        self._connection = connection

    def execute(self, sql: str, parameters: SqlParameters = (), *,
                write: bool = False) -> QueryResult:
        self.check(write)
        try:
            return SessionQueryResult(self._connection.execute(sql, parameters), self.check)
        except sqlite3.IntegrityError as exc:
            raise Failure("constraint_conflict") from exc
'''
s=original[:a]+adapter+original[b:]
s=s.replace('from dataclasses import replace\n','').replace('from msg.core.codec import canonical, decode, digest, loads, wire\n','').replace(models,'')
s=s.replace('from msg.core.errors import Failure, require','''from msg.core.errors import Failure, require
from msg.core.query import QueryResult, SqlParameters
from msg.storage.query import SessionQueryResult
from msg.storage.session import RelationalSession''')
p.write_text(s)
p=r/'storage/postgres.py';s=p.read_text().replace('from msg.storage.sqlite import SqliteSession','''from msg.core.query import QueryResult, SqlParameters
from msg.storage.query import SessionQueryResult
from msg.storage.session import RelationalSession''')
s=s.replace('class PostgresSession(SqliteSession):','class PostgresSession(RelationalSession):')
s=s.replace('super().__init__(connection, write=write)','super().__init__(write=write)\n        self._connection = connection')
s=s.replace('def execute(self, sql, parameters=(), *, write=False):','def execute(self, sql: str, parameters: SqlParameters = (), *,\n                write: bool = False) -> QueryResult:')
s=s.replace('''            return self._connection.execute(
                _postgres_sql(sql, has_parameters=bool(parameters)), parameters or None)''','''            cursor = self._connection.execute(
                _postgres_sql(sql, has_parameters=bool(parameters)), parameters or None)
            return SessionQueryResult(cursor, self.check)''')
p.write_text(s)
(r/'core/query.py').write_text('''"""Explicit SQL query port for trusted server-side domain consumers.

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
    None | bool | int | float | str | list[SettingValue]
    | tuple[SettingValue, ...] | Mapping[str, SettingValue]
)


class QueryResult(Protocol):
    @property
    def rowcount(self) -> int: ...
    def fetchone(self) -> SqlRow | None: ...
    def fetchall(self) -> list[SqlRow]: ...
    def __iter__(self) -> Iterator[SqlRow]: ...


@runtime_checkable
class QuerySession(Protocol):
    def execute(self, sql: str, parameters: SqlParameters = (), *,
                write: bool = False) -> QueryResult: ...
    def one(self, sql: str, parameters: SqlParameters = ()) -> SqlRow | None: ...
    def rows(self, sql: str, parameters: SqlParameters = ()) -> list[SqlRow]: ...
    def setting(self, key: str, default: SettingValue = None) -> SettingValue: ...
    def set_setting(self, key: str, value: SettingValue) -> None: ...
''')
(r/'storage/query.py').write_text('''"""Session-bound query results without a public driver cursor/connection."""
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
''')
p=r/'core/contracts.py';s=p.read_text().replace('from .models import *','from .models import *\nfrom .query import QuerySession').replace('class MetadataSession(Protocol):','class MetadataSession(QuerySession, Protocol):');p.write_text(s)
p=r/'market/policy.py';s=p.read_text().replace('from msg.core.errors import require','from msg.core.errors import require\nfrom msg.core.query import QuerySession').replace('def load_policy(tx, policy_id):','def load_policy(tx: QuerySession, policy_id: str):');p.write_text(s)

p=Path('docs/ISSUE_PROGRESS.md')
s=p.read_text().replace('- [ ] 中立共享 RelationalSession','- [x] 中立共享 RelationalSession').replace('- [ ] 显式 QuerySession/QueryResult','- [x] 显式 QuerySession/QueryResult')
s+='''
## 实现切片（尚待云端验收）

SqliteSession/PostgresSession改为同一RelationalSession的兄弟类；共用域方法不再持有连接或导入数据库驱动。所有原SQL、schema、迁移、隔离、写栅栏、恢复补偿、提交后信号保留。QuerySession显式列出实际查询方法；MetadataSession继承它，真实market.policy.load_policy以该接口标注。

execute返回受会话约束的QueryResult而不是驱动游标，不公开connection/commit；rowcount/fetchone/fetchall/迭代均重验任务和生命周期。没有把进程内Python当作安全沙箱，没有增加ORM/逐表Repository/任意SQL网络接口。

实现待同一红基线用例、真实PG/SQLite、账本、回滚补偿和完整四分片验收。所有执行在云端，未进行本地项目测试。
'''
p.write_text(s)
