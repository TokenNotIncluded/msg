# Architecture issue progress — 2026-09-29

## Source and working boundary

- Repository: TokenNotIncluded/msg.lmm.best; coordinator: #83; architecture umbrella: #85.
- Starting main: `476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7`, tree `40ca207a3c49be347120e058e47ed328ec4e3098`.
- Independent branch: `fix/architecture-boundaries-20260929`. Do not write or force-push the recovery branch for #155.
- Main CI runs 36472507327 (complete contracts) and 36472507300 (recovery) were read as success. Shard-0 source artifact 10992168743 was downloaded and its SHA-256 verified as `9abbc69988b8ffcb45f9931dc87e291145a7680f275bf7aadbedb7bed2189d10`. This is baseline evidence, not evidence for new changes.
- Follow the authoritative design and valid completed archive linked by #83. Preserve historical schemas/signatures, current authorization, one transaction publisher, quarantine and lease fences.

## Active slice: #160

Observed: `ToolExecutor.invoke(...) -> ResourceRef` has no consumer; the worker actually injects an async callable returning a local `ToolResult`. That result is owned by `workers.effects`, forcing the sandbox adapter to import its orchestrator.

1. Regression-first commit: new ownership/import/type/assembly tests plus real PostgreSQL worker success and invalid-result refusal. Existing tool and completion-fence tests remain unchanged.
2. Cloud-only execution: `.github/workflows/architecture-boundaries.yml` records exact commit/tree and uploads JUnit; it does not replace the full four-shard/conformance/build gate.
3. Planned minimal change: neutral `core.tool_execution` owns a callable ToolRunner and local ToolResult; worker and sandbox consume them. Keep the existing effects.ToolResult import as the same type. Retire the unused invoke declaration explicitly, retaining ToolExecutor only as an import alias for the actual runner port.
4. Pending: read the red run, implement, read focused and full final-head evidence, review diff and merge only if the exact combination is verified.

No implementation success or issue closure is claimed by this regression-first note.

## Queue and separate work

- #156: executor currently imports batch, discovery and communication helpers and retains Application; needs explicit narrow assembly dependencies, not just one renamed import.
- #157: PostgreSQL inherits concrete SqliteSession; separate genuinely neutral session behavior without changing SQL/locks/schema.
- #158: client imports server executor/Git/vault helpers; minimal-client packaging remains a distinct acceptance requirement.
- #159: market/plugin bidirectional helper ownership requires preserving all published order versions and the sole protected ledger path.
- #155: live head observed `0e4dbe777053e354bcf4845b851a0c9b86ab8310`; title says verifier fix submitted while body still describes older failed head. Do not infer success; refresh its actual diff/CI and resolve main conflicts in a separate serial integration.
- Functional umbrella issues #68–#83/#85 remain open unless every remaining acceptance item has exact evidence. Existing completed functionality must not be rewritten from stale issue descriptions.
- #64/#65/#84 need authorized production logs/configuration, real protected snapshots and field/capacity evidence; #68/#69/#70 also retain external trust/retirement/console requirements. Code CI cannot supply those facts.

No deployment, production Root/PIN, real money, outbound notification, key destruction or recovery promotion is authorized or performed.
