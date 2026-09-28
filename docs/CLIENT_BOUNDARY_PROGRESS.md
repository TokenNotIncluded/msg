# Client dependency boundary progress — 2026-09-29

Refs #158/#82/#83/#85. Branch `fix/client-boundary-20260929` starts at tool head f14fd1533f70983b046df4b4c813993e8a536d12. #165 was subsequently merged as 08c58928ee3f6c6cb350d9e9fba75d072bd78f08 with the same exact tested tree; #160 is closed. Do not write #155 recovery, #169 executor or #166 metadata-session branches from this slice.

## Independently verified RED

- Head **6ea463d6fa8b256cb07d0ede2d7795a5336eda4f**, tree **2a89a0a36f2d87646cccd4fdbd5ed8420f31c9b9**.
- Cloud run **36483229167**, artifact **10998106395**, SHA-256 `449fc080ba7232cceea4c6f6444d1468fda03b6278112d1c9cbec96dcfdce112` was downloaded and independently verified. Source JSON matches this commit/tree; JUnit reports **7 failed / 0 errors / 0 skipped**, including the actual client import guard refusing server vault imports. This is expected regression-first failure, not a successful implementation.
- The real baseline wheel was built; minimal installation remains a failure because its base dependencies include server packages. No project code/test/build ran locally.

## Reviewed implementation

A fixed-parent one-shot cloud patch applies only the 19 enumerated source/install-document files on this feature branch, with no force update or project execution under write credentials. The temporary publisher and patch are removed from the resulting source tree. Exact generated head/tree and artifact hash must be read before final PR verification.

The change moves compact result encoding, atomic local-file replacement, state-free custodial proof/statement bytes and MCP version constants into their actual shared owners, retaining identity-preserving compatibility exports. Clients no longer import executor, Git storage or server vault for those helpers. Published bytes, credential file protections and server-held authority/state stay unchanged. Base metadata becomes client-only; server extra holds the original server constraints, dev still includes server, and daemon commands fail clearly before state creation when those dependencies are absent. See CLIENT_INSTALLATION.md for migration instructions.

## Remaining gates

The independent workflow must prove all seven checks, real wheel outside the checkout, no server-only Python dependencies, both help entrypoints and four actual signed transport calls using an in-process mock endpoint. Full original server, journal/recovery, protocol and four-shard exact-node-ID/build gates remain mandatory. Review and verify the final combination with current main/#169 rather than treating this isolated branch as the combined result. No GREEN, merge or #158 closure is claimed at this checkpoint.

No production deployment, Root/PIN, funds, real external notification, key destruction or recovery promotion was performed. Production and platform-field issues remain blocked on their own evidence.
