# Client and architecture integration progress — 2026-09-29

Refs #158/#156/#157/#82/#83/#85. PR #171, branch `fix/client-boundary-20260929`. #167 is the single executor/session implementation; #169 preserves alternative source/evidence, not a second implementation to merge. No other work stream's ref was changed.

## Current checkpoint

The preceding combined head **3ae9f8efeed9b936f851a79c29cdaab2bf84a267**, tree **6e7f84e2686170720dd408798422b9fcc5a310d4**, joined client5f56bbe4 and #167e768cbcf. After the parallel #167 branch advanced to **2d1cc443c828dd2f735995cc6a5d20021fee6ac7**, its complete diff from e768cbcf was inspected. It changes only five workflow/test/note files, retains the exact product source, expands the storage gate's migration/ledger/recovery coverage, and adopts the same complete frozen-JSON comparison correction.

This next two-parent integration preserves 3ae9f8ef and 2d1cc443 histories, adopting those five exact blobs. All #167 source and supplemental #166 tests are retained; the #169 alternative executor is not copied. The client result helper still has one implementation: **core.codec.result_wire**, re-exported as the same object from **core.packet**; executor keeps its packet import. No packet schema/decoding or signed bytes change.

The final combined commit/tree must pass new cloud gates; its exact ID is in the commit and PR #171. Neither an older isolated green nor pending/cancelled runs certify it. The pre-sync3ae workflows were full36486574058, architecture36486574116, storage36486574157, tool36486574150, client36486574123 and recovery36486574183; read the new head's own runs instead before merging.

## Completed main slice

#165 merged as **08c58928ee3f6c6cb350d9e9fba75d072bd78f08**, exact tree **8a62150d8273a2c75775872694749cb529835966**; #160 is closed. Exact-tree evidence: **1,860 full tests, 8 conformance, 38 focused**, recovery safety and wheel/sdist success. Four-shard actual-node-ID union/disjointness and artifact hashes were independently checked. Full evidence is in #165; this integration preserves that tool contract.

## Client RED and independently verified isolated GREEN

- RED **6ea463d6fa8b256cb07d0ede2d7795a5336eda4f**, tree2a89a0a36f2d87646cccd4fdbd5ed8420f31c9b9, run36483229167: **7 failed /0 errors /0 skips**. Artifact10998106395 SHA-256 `449fc080ba7232cceea4c6f6444d1468fda03b6278112d1c9cbec96dcfdce112`, source and JUnit verified. Actual fresh imports rejected server vault; baseline wheel failed minimal dependency acceptance.
- Reviewed source patch SHA-256 `244778f572724b991a8b14ff3334b810a3af14329de999bf148c5445950c8a02`. First cloud application36484923358 refused normalized-sdist TOML formatting before publication. Commit649a85da applied the same dependency split directly to actual repository-format metadata; no assertion was relaxed.
- Successful cloud application36485787986 produced **5f56bbe4ea1cddc46bed12d29026589d65485d62**, tree **0b339516eb31ddef8f3aa576857710706cb82a97**. Artifact10999476065 SHA-256 `b43c4f67d52fc1a27739d2ad4ee1d772bb62b4ff105419b93c3f3f84285b3617` matches source, reviewed patch and18 remaining paths. Temporary patch/publisher were removed from the resulting tree; no project code executed under its write credentials.
- **Isolated client GREEN** run36486003907, synthetic mergec4a5c34b16171333ad05803a43a145c93a4e5985, exact tree0b339516 above. Artifact11000036526 SHA-256 `5fb24ee583c5c9f76b0255475b9c7dea76d587c0c74fe8cc3fafa7f65e887810` independently downloaded and checked: **7 passed /0 failures /0 errors /0 skips**; real wheel/sdist; clean normal wheel installation outside checkout, pip check and both help entrypoints; installed package manifest excludes all seven server-only packages; strict fresh-interpreter guard reports no server imports; four actual HTTP/path-GET/GraphQL/MCP transports exchange the same verified signed packet through in-process MockTransport. This is evidence for the isolated source, not the later combined head.

## Compatibility and retained regressions

Atomic file replacement, state-free custodial proof/decision/ACK bytes and MCP version constants have neutral owners with same-object compatibility exports. Base installation is client-only; server extra retains original server constraints and dev retains full server. Daemon service commands fail before local state when dependencies are absent. CLIENT_INSTALLATION.md and deployment/README document the required explicit server installation migration. No authority, isolation, journal, crypto purpose, historical version, transaction or publication fence is relaxed.

The transferred standalone assembly assertion compares the complete `wire(result.data)` because domain JSON is frozen. #169 corrected head3db973ab1784926eb4e545cabdf832e7faa0a4bc had independently verified **34 passed /0 failures/errors/skips** in focused36484427064 (artifact10999215628 SHA-256 `fcbe6d55cba0665d227cef3ae09ca5f256756cd90d86f5394ebf499fe95a91eb`). #1672d now contains that same correction, preserving batch replay and Event/business/result transaction rollback checks.

## Remaining gates and separate work

Current combined-head client wheel/import/four-transport checks, architecture/storage/tool/recovery suites, full four-shard exact-node-ID union, conformance/real-tokenizer and build remain required. Refresh main/#167, inspect final diff and exact merge tree before serial integration. Keep #171 draft and #158 open until those gates and review complete. Do not infer Windows/Android, external network, performance or production acceptance from isolated Linux CI.

#155 corrected isolated head0e4dbe777053e354bcf4845b851a0c9b86ab8310 had independently verified **55 passing recovery tests**, run36480031762/artifact10995627663/hash `e200808e372cc7eeab5e688fa29948fbd0c269261727837879294969c1f838d6`. Its stale failure body was corrected, but current-main conflict/combined acceptance remain separate. #168 market and #161–#163 remain separate work. #64/#65/#84 and remaining live Root/trust/retirement/console requirements need authorized evidence, not a CI substitute.

All project execution/tests/builds run in cloud. Local work is source text/Git-object review and downloaded artifact verification. No production deployment, real Root/PIN, funds, actual outbound delivery, key/backup destruction or recovery promotion was performed.
