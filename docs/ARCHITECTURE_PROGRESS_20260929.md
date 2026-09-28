# Architecture issue progress — 2026-09-29

## Source and working boundary

- Repository: TokenNotIncluded/msg.lmm.best; coordinator: #83; architecture umbrella: #85.
- Starting main: `476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7`, tree `40ca207a3c49be347120e058e47ed328ec4e3098`.
- Main CI runs 36472507327 (complete contracts) and 36472507300 (recovery) were read as success. Shard-0 source artifact 10992168743 was downloaded and hash verified: `9abbc69988b8ffcb45f9931dc87e291145a7680f275bf7aadbedb7bed2189d10`. Baseline evidence does not validate later changes.
- Follow the authoritative design and valid completed archive linked by #83. Preserve historical schemas/signatures, current authorization, one transaction publisher, quarantine and lease fences. Never force-push another work stream or treat an old GREEN as the current combination.

## #160 — tool runner boundary, PR #165

Implemented on `fix/architecture-boundaries-20260929` at **f14fd1533f70983b046df4b4c813993e8a536d12**, tree **8a62150d8273a2c75775872694749cb529835966**.

The neutral `core.tool_execution` owns callable ToolRunner and local ToolResult. EffectWorker and BubblewrapRunner share the actual contract. The effects.ToolResult export remains the same type; ToolExecutor is an explicit compatibility alias, not a second unused invoke-to-ResourceRef contract. Existing worker authorization, mandatory sandbox, output and completion fences are unchanged. See `TOOL_RUNNER_BOUNDARY.md`.

- RED commit b553c59b484a4e5a383df3500ad9e4cab9d51272; run **36480982432**: **7 failed / 31 passed**. Old tools/fence tests passed.
- Focused GREEN PR run **36481370662**: downloaded artifact **10996748629**, SHA-256 `6a86edb23bac1f6463a65ca9b1118a9c54b0b25c274b81539defdc8ee0e03e9f`. Independently checked JUnit **38 tests / 0 failures / 0 errors / 0 skips**. Synthetic merge 0af4b1910bd31615e0203ae1cce5571b8a048142 has the exact implementation tree above.
- Recovery safety run **36481370672** was read as success. Full run **36481370643** still in progress at this checkpoint. No merge or closure is claimed by this note; refresh final PR/base/CI before acting.

## #156 — executor boundary

Separate stacked branch `fix/executor-boundary-20260929`. RED run **36481661656** independently verified **9 failed / 25 passed / 0 errors / 0 skips**. Implementation applied at c639a520ab69e22cccaabb07999d7cb54b8be4ca and final-head verification remains pending. See `EXECUTOR_PROGRESS_20260929.md` for exact artifacts and ownership. This removes all three categories of core-to-plugin dependency, not just one renamed import.

## Other queue and parallel work

- #157 is being handled separately by PR **#166**, metadata-session invariants versus concrete SQLite/PostgreSQL adapters. Do not duplicate or overwrite that branch.
- #158 now has independent branch `fix/client-boundary-20260929` with regression-first client/wheel/import tests at 6ea463d6fa8b256cb07d0ede2d7795a5336eda4f. Minimal packaging and actual four transport calls must be proved; no client-boundary success is claimed here.
- #159: market/plugin helper ownership remains open; preserve published order versions and sole protected ledger path.
- New #161–#163 need their own source/overlap review; do not infer they are resolved by the above module moves.
- #155 recovery head last read as 0e4dbe777053e354bcf4845b851a0c9b86ab8310 while body retained an older failure. Refresh actual diff/review/CI; do not infer success or rewrite its active branch.
- Functional umbrellas #68–#83/#85 remain open unless every remaining acceptance item has exact evidence. Do not reconstruct completed functionality from stale issue bodies.
- #64/#65/#84 need authorized production logs/configuration, real protected snapshots and field/capacity evidence. #68/#69/#70 also retain external trust/retirement/console requirements. Code CI cannot supply those facts.

No deployment, production Root/PIN, real money, outbound notification, key destruction or recovery promotion is authorized or performed. All project execution/testing in this work stream is cloud-only; local work is source inspection/editing and artifact verification.
