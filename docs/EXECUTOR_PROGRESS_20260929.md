# Executor architecture progress — 2026-09-29

Refs #156/#83/#85. Branch `fix/executor-boundary-20260929`, PR #169. Its prerequisite #165 was merged as 08c58928ee3f6c6cb350d9e9fba75d072bd78f08 with exact tested tool tree 8a62150d8273a2c75775872694749cb529835966; #160 is closed. Do not overwrite #155/#166 or other work streams.

## Evidence and current state

- RED: 1953beeda08cb34570f2c56535c4e4b1249546eb, tree 28cd98e8d3cb1e54df9714efc29ea06a41107898, cloud run36481661656. Artifact10996698881 SHA-256 `0dfce2e01becb21a5660017ddc017d2ad3102cdc7cb98b99c86a42bdf1f1e8e9`; independently inspected source/JUnit: **9 failed / 25 passed / 0 errors / 0 skips**.
- Reviewed implementation patch SHA-256 `0389f11f5609abc7c54ecf67159b6aae75245d81c94611c8a4ecf7017f3ed3dc` was applied by cloud run36482413751 only to its nine enumerated source paths. Generated commit c639a520ab69e22cccaabb07999d7cb54b8be4ca/tree07ad9c86bf26b7d9e3b294200ebd9709f7e5f888. Artifact10998370091 SHA-256 `9914220c3ff71d1dcca02491a3cada0dd95e4babf06b1838f9b09298a827a4e9` matched source, paths and patch. The temporary patch publisher and patch are absent from the final tree; it did not execute project code with write credentials.
- First implementation/note head f7f8a890d91e4eda5d3da5f3c2ac4d8988e70848/treea9e18de677098798cc9f518cdd9ccb4c1a82c336: focused run36483396339 produced **2 failed / 32 passed / 0 errors / 0 skips**. Artifact10997282312 SHA-256 `d726cd26b800c6415ef7f0edc8c94f5a39f7509036fa66c8590f0cf9d79c26ca`, source/JUnit independently inspected. Recovery36483396239 and tool-boundary36483396136 succeeded; full gate was still queued. That head is **not GREEN**.
- Two precise corrections follow: the new standalone test compares the entire wire representation because models freeze nested lists/maps; the composition root uses a live module binding for Event projection rather than capturing a function with partial, retaining the existing post-load fault-injection/instrumentation contract. Existing webhook rollback assertions are untouched. This commit requires a new final-head cloud run; success is not yet claimed.

## Implemented ownership

- `core.batch.BatchPolicy` owns the single child-admission algorithm and consumes explicit bounded `PacketDecoder`. Atomic/independent batch size, idempotency and secret-delivery rules stay unchanged.
- `core.events.event_id` owns the unchanged deterministic digest; communication has a compatibility import of that exact function.
- `OperationExecutor` has explicit decoder/byte-limit, `ProjectionReader` and in-transaction `EventProjector` dependencies. It has no Application/settings backreference or business-plugin/transport implementation imports.
- `Application.make_executor` composes normal and forced-SSH execution with their respective authenticator and existing recovery/secret-release fences. It exposes finite trusted services, not a resource-configured hook engine.
- Missing decoder/projection fails closed with stable errors. Event callback failure rolls back business state, Event and request result in the existing transaction. No second writer/transaction is introduced.
- The issue_78_80 test-only harness supplies its decoder/projection explicitly. No original behavioral assertion was deleted or weakened.

## Remaining gates

Read corrected focused/full final-head artifacts, review the full diff and exact current-main merge tree, then decide #156 closure. All project execution/tests are cloud-only. No production/log/snapshot/Root/PIN/funds/outbound/recovery-promotion work was performed; production-blocked issues remain blocked.
