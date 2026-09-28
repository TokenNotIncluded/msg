# Metadata session boundary progress

## Coordination and baseline

- Issue #157 only; broader #65/#69/#85 production and architectural acceptance remain separate.
- Base main: `476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7`, tree `40ca207a3c49be347120e058e47ed328ec4e3098`.
- Branch: `fix/metadata-session-boundary-20260929`.
- #83 comment 5878201914 records the coordination change. The overlapping regression-only #164 is closed without merging; #165 owns #156/#160 and #155 owns recovery. Do not overwrite either branch.
- Main source was read from CI36472507327 artifact10992168743. Its downloaded SHA-256 matches `9abbc69988b8ffcb45f9931dc87e291145a7680f275bf7aadbedb7bed2189d10`.

## Stage 1: regression-first

This commit adds 4 architectural contracts and 6 shared behavioral tests, each behavioral test parameterized over real PostgreSQL and SQLite (16 tests total). Source implementation is unchanged. The focused cloud workflow records exact commit/tree and JUnit; no new pass result is claimed before execution.

Coverage: independent PostgreSQL import, neutral ownership, declared MetadataSession methods, resource generations/path/paging, persisted result/event/audit/job identities, savepoint compensation, read-only/closed/cross-task guards, unique-constraint rollback and driver parameter/literal handling.

## Planned boundary

Before: `PostgresSession -> SqliteSession -> SQLite connection + shared model operations`.

After: PostgreSQL and SQLite independently implement the driver operation of a neutral MetadataSessionBase. That base owns only model/session invariants and rollback-callback ordering; it does not own a connection, transaction commit, schema, migration, placeholder translation or driver exception handling.

PostgreSQL keeps its direct/session-pooled connection requirement, connection-scoped advisory lock held through compensation, nested savepoints, pending-effect signals only after commit and exact SQL/error adapter. SQLite keeps its own connection, pragmas, error mapping, writer file fence and test/fake stores.

## Required evidence before closure

Obtain actual red evidence, then apply the extraction without schema/migration/domain changes. Run the shared contracts on both adapters, the existing PostgreSQL concurrency/migration/recovery suites, and the complete four-shard node-ID/conformance/build gate on the final head. Verify the tested tree before merging. Production restore/backup/operator evidence is not simulated or marked complete by these tests.


## Red evidence and implementation

Focused run **36481798533** tested exact regression commit
`7f8752e37fe5b5a8021f192b87e731caec2bbc2d` / tree
`686dbddc1bdfc52d97c127bdb7338f002cabf735`: **4 failed, 12 passed,
0 errors, 0 skipped**. All failures were new architecture assertions;
all six shared behavior tests passed independently on both adapters.
Artifact10997260030 was downloaded and its SHA-256 matched the provider:
`50ae1ff1b71684d60e0ff133cde41c6cb81658c0855aeb9c376fff110dc27e2e`.

The extraction changes only `storage/session.py`, `storage/sqlite.py`
and `storage/postgres.py`. `MetadataSessionBase` now owns the existing
resource/identity/result/event/audit/job methods, task/read-only guards
and rollback compensation. Its sole abstract method is bound statement
execution; `SqlCursor` explicitly describes the small real cursor port.
Both concrete sessions own their own driver handle and statement adapter.

`core.contracts.MetadataSession` remains the structural business-facing
contract and is not weakened. Existing storage-local helpers (`one`,
`rows`, `check`, path/ancestor lookup, settings and job updates) remain
explicit concrete session methods; they do not grant callers commit
rights. The internal qmark convention is translated only by each driver.
No new ORM, repository service, configuration matrix or migration exists.

The assembly workflow checks exact input blob IDs and compares every
extracted method except the constructor/statement port with the original
method AST. It also checks both complete Store classes, FakeMetadataStore,
driver execute/enqueue methods, PostgreSQL SQL translation, and both
schema literals for exact structural equality. It uploads this audit,
source patch and source commit/tree. Assembly is not a substitute for
green cloud tests. One-shot tooling stays on the build branch and is
removed from the submitted tree before final validation.

PostgreSQL connection-scoped advisory locking through compensation,
savepoints, cancellation handling, migration, SQL/error adaptation and
post-commit effect signals are unchanged. SQLite retains its own file
writer fence, pragmas, connection, schema and exception mapping. No schema,
historical ID, ledger entry, signature or receipt bytes are rewritten.

Final focused/full/recovery results are still pending at this commit.
Do not close #157 or broader production-evidence issues from this stage.
