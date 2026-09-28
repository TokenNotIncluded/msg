# Client and architecture integration progress — 2026-09-29

Refs #158/#156/#157/#82/#83/#85. This branch belongs to PR #171 (`fix/client-boundary-20260929`). #167 is the single executor/session implementation; #169 is preserved as alternative source/evidence, not a second implementation to merge. No other work stream's ref was changed.

## Completed independent baseline

#165 merged as **08c58928ee3f6c6cb350d9e9fba75d072bd78f08**, exact tree **8a62150d8273a2c75775872694749cb529835966**; #160 is closed. Its exact-tree gate had 1,860 full tests, 8 conformance, 38 tool/fence tests, recovery safety and wheel/sdist success. Four-shard actual-node-ID union and artifact hashes were independently checked; detailed evidence remains in #165.

## Client RED and implementation

- RED6ea463d6fa8b256cb07d0ede2d7795a5336eda4f/tree2a89a0a36f2d87646cccd4fdbd5ed8420f31c9b9, run36483229167: **7 failed /0 errors /0 skips**. Artifact10998106395 hash `449fc080ba7232cceea4c6f6444d1468fda03b6278112d1c9cbec96dcfdce112` and exact source/JUnit independently verified. Actual fresh client imports rejected server vault; the built wheel also failed minimal dependency acceptance.
- Reviewed source patch hash `244778f572724b991a8b14ff3334b810a3af14329de999bf148c5445950c8a02`. First application run36484923358 refused the pyproject hunk before publication because the sdist had normalized the repository's TOML formatting. Commit649a85da210bdc5d13f84a4339719f9628b23b7f directly committed the same dependency split against the actual file. No assertion was relaxed.
- Cloud source application36485787986 succeeded, producing **5f56bbe4ea1cddc46bed12d29026589d65485d62**, tree **0b339516eb31ddef8f3aa576857710706cb82a97**. Artifact10999476065 hash `b43c4f67d52fc1a27739d2ad4ee1d772bb62b4ff105419b93c3f3f84285b3617` was checked against source, exact patch and18 remaining paths. The one-shot publisher/patch are absent from the resulting tree. No project code executed with its write credentials.
- PR171 opened on that isolated implementation and triggered client36486003907, full36486003891, recovery36486003883 and tool36486003903. Do not infer final integration success from those isolated runs.

## Explicit two-parent integration

This integration preserves both client5f56bbe4 and #167's **e768cbcf761c11a65b4cc02d0ee472791a976a3d** (tree **cea28492703780de7b7988917136dffde40a56c8**) histories. It retains #167's complete executor, neutral packet/schema, query/session implementations and all migrated tests, plus the verified #165 tool boundary. No #169 alternate BatchPolicy or make_executor implementation is copied in.

Client changed blobs are reused verbatim from the reviewed source application, except the common result serialization is reconciled: **core.codec owns the one result_wire function; core.packet re-exports that exact function; executor continues its existing core.packet import.** CLI and MCP use codec directly. There are no two definitions or circular imports, and both compatibility/architecture identity tests remain. Packet envelopes, bounded decoding and errors stay in core.packet unchanged.

The standalone assembly test transferred from #169 receives its exact already-reviewed correction: compare `wire(result.data)` against the entire expected nested JSON. This does not weaken its value/transaction checks. The correction came from3db973ab1784926eb4e545cabdf832e7faa0a4bc, whose focused run36484427064 was independently verified **34 passed /0 errors /0 failures /0 skips** (artifact10999215628 hash `fcbe6d55cba0665d227cef3ae09ca5f256756cd90d86f5394ebf499fe95a91eb`). #167 was notified; its branch is not changed here.

## Required final gates

The new combined commit/tree, not any historical green, must pass actual seven client checks, fresh dependency-clean wheel install and four signed mock-HTTP transport calls, architecture/storage/tool/recovery focused suites, full four-shard exact-node-ID union, conformance/real-tokenizer and build. Refresh #167/main before serial integration. Keep #171 draft and #158 open until the final combination is verified and reviewed. Installation role/migration details remain in CLIENT_INSTALLATION.md; core.packet is an additional identity-preserving result_wire export.

#155's corrected isolated head0e4dbe777053e354bcf4845b851a0c9b86ab8310 was independently checked as55 passing recovery tests (run36480031762, artifact10995627663 hash `e200808e372cc7eeab5e688fa29948fbd0c269261727837879294969c1f838d6`); its old failure body has been corrected, but main conflict/combined acceptance remain separate. #168 market and new #161–#163 are separate work. #64/#65/#84 and remaining live Root/retirement/console acceptance require authorized evidence, not a CI substitute.

All project execution/tests/builds in this work stream run in cloud. Local work is source inspection/text/Git-object construction and downloaded artifact verification. No deployment, real Root/PIN, money, actual outbound delivery, key/backup destruction or recovery promotion was performed.
