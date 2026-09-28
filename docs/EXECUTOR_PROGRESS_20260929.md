# Executor architecture progress — 2026-09-29

Refs #156/#83/#85. Independent stacked branch `fix/executor-boundary-20260929`, starting from tool boundary implementation `f14fd1533f70983b046df4b4c813993e8a536d12` (PR #165). Do not overwrite #165/#155/#166 branches. This note supplements `ARCHITECTURE_PROGRESS_20260929.md`.

## Evidence and current state

- RED: `1953beeda08cb34570f2c56535c4e4b1249546eb`, tree `28cd98e8d3cb1e54df9714efc29ea06a41107898`, cloud run **36481661656**. Downloaded artifact **10996698881**, SHA-256 `0dfce2e01becb21a5660017ddc017d2ad3102cdc7cb98b99c86a42bdf1f1e8e9`. Source JSON and JUnit independently inspected: **9 failed / 25 passed / 0 errors / 0 skips**. All failures were the new boundary/assembly/error tests; existing batch, secret-release, domain-webhook, SSH and transfer-rollback tests passed.
- Reviewed implementation patch SHA-256: `0389f11f5609abc7c54ecf67159b6aae75245d81c94611c8a4ecf7017f3ed3dc`. Cloud run **36482413751** applied only the nine enumerated paths to the fixed parent and non-force updated this feature branch. Its final commit is **c639a520ab69e22cccaabb07999d7cb54b8be4ca**, tree **07ad9c86bf26b7d9e3b294200ebd9709f7e5f888**.
- Patch-result artifact **10998370091**, SHA-256 `9914220c3ff71d1dcca02491a3cada0dd95e4babf06b1838f9b09298a827a4e9`, was downloaded. Its source, path allowlist and patch hash match the reviewed change. The one-shot publisher and patch are removed from the final tree and remain inspectable in history. No project code was executed by that write-enabled job.
- This note commit follows the implementation. Focused/full CI must run against the resulting final head and actual main combination. **GREEN is not yet claimed.** Existing full four-shard/conformance/build CI remains mandatory and unchanged. No project tests were run locally.

## Implemented ownership

- `core.batch.BatchPolicy` owns the single child-admission algorithm and consumes an explicit bounded `PacketDecoder`. Atomic and independent batch primitives retain their existing size/idempotency/secret-delivery rules.
- `core.events.event_id` owns the unchanged deterministic digest. Communication keeps a compatibility import of that same function.
- `OperationExecutor` receives packet decoder/byte limit, `ProjectionReader` and in-transaction `EventProjector`. It has no Application/settings backreference and no imports of business plugins or transport implementations.
- `Application.make_executor` is the composition root. Normal and forced SSH execution use it with their respective authenticator and existing recovery/secret-release fences. A standalone executor can use its own explicit services.
- Missing decoder/projection is a stable fail-closed error, not an internal error or implicit authorization fallback. Event callback failure still rolls back the existing business/Event/result transaction; no separate writer, transaction or configurable hook engine was added.
- The test-only issue_78_80 harness now provides its decoder and projection explicitly. No existing behavioral assertion was deleted or weakened.

## Remaining gates

Read focused/full final-head artifacts, review the final diff and exact merge tree, then decide #156 closure. #165 must be integrated first or included in that exact tested combination. #155 recovery and #166 metadata-session changes require their own reviewed integration. No production/log/snapshot/Root/PIN/funds/outbound/recovery-promotion work was performed; production-blocked issues remain blocked.
