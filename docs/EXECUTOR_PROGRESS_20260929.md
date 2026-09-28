# Executor architecture progress — 2026-09-29

Refs #156/#83/#85. Independent stacked branch `fix/executor-boundary-20260929`, starting from tool boundary implementation `f14fd1533f70983b046df4b4c813993e8a536d12` (PR #165). Do not overwrite #165 or #155 branches. This note supplements `ARCHITECTURE_PROGRESS_20260929.md`.

## Regression-first

New tests expose core imports of plugin implementations and the Application backreference, inconsistent standalone/factory assembly, and missing narrow services reported as internal errors. Real PostgreSQL tests retain batch idempotency, write projections, and verify Event projection failure rolls back both business state and Event/result in the existing transaction. Unchanged batch secret-delivery, webhook domain, SSH and transfer rollback tests run alongside them in `.github/workflows/executor-boundary.yml`.

This commit only introduces tests/workflow/notes. No success is claimed before cloud execution. Existing full four-shard/conformance/build CI remains mandatory and unchanged.

## Planned ownership

- Core BatchPolicy owns the single child admission algorithm and consumes an explicit bounded packet decoder; core must not import a transport or plugin to find one.
- Core event identity owns the existing deterministic digest; communication keeps only a compatibility import.
- OperationExecutor receives a packet decoder/byte limit, read-projection function and in-transaction Event projection function. It holds no Application/settings backreference.
- Application is the composition root for those dependencies; normal and SSH entrypoints use the same factory with their own authenticator and unchanged recovery/secret-delivery fences.
- Missing decoder/projection is a stable fail-closed error, not an internal error or implicit authorization fallback. Event delivery effects stay in the existing transaction; this is not a configurable workflow/hook engine.

## Remaining evidence

Read RED result, implement without weakening old assertions, check focused/full final head and reviewed exact combination. #156 remains open until those gates pass. No production/log/snapshot/Root/PIN/funds/outbound/recovery promotion work is performed. Production-blocked issues remain blocked.
