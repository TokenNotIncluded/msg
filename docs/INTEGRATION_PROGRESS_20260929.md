# Issue integration notes — 2026-09-29

All project code execution, tests and builds run in GitHub Actions. Local work is source/Git/AST inspection and independent artifact parsing only. No production deployment, Root/PIN/key read, real funds/outbound delivery or restore promotion.

## Completed on main

- #156/#157: #167 merged at 261c8816; parallel flow independently audited 1894 real tests and conformance8 before closing both architecture issues. #169 superseded with its distinct regressions retained.
- #162: #172 originally merged into an old feature branch, not main. Promotion #175 fixed the target and merged as **edbfbae7648529c6b08a2e81a1a10bb349f6b50d**, tree **ebe1facca432803854d0c04aaa93519a589ff93f**. Independently inspected focused122 plus full1901 (465/496/483/457), exact node-ID union, no failure/error/skip/duplicate/missing; conformance8, measured token budget and wheel/sdist passed. Full36551495771; focused36551495692. Hashes/source identity: #175 final body and #162 comment5888054193. Original token RED/history: TOKEN_DELIVERY_BOUNDARY_PROGRESS.md.
- #161: #173 merged as **04ffad5834dcc959f5ec59696bb264c3d527990b**, tree **f377ce7dbdb53e0480552906a7d7529eee67171f**. Actual merged tree matches the verified combination. Focused hosting41 and same-head token122; full **36553079854 = 1935** (475/506/492/462), exact node-ID union, zero failure/error/skip/duplicate/missing. Conformance8, measured token budget, wheel/sdist and contracts gate passed. Original #174 independent UID/permission tests, its nine-line owner guard and real history are retained; only the duplicate product PR was closed. Exact final proof: #173 body and #161 comment5888274187. Historical RED/GREEN and deployment model: HOSTING_RUNTIME_PROGRESS.md, HOSTING_AUTHORITY_PROGRESS.md, HOSTING_RUNTIME.md.

No source test result is represented as production acceptance. Main after these merges is 04ffad5834dcc959f5ec59696bb264c3d527990b.

## Client integration — #171 / #158

Previous client head **730780abf1b25b91a972dc4a9cccf0cf17408728**, tree **758e86821ee31f59afbed22935207fad6cd5fb11**. Its old PR body referred to an earlier head. Full/client/architecture/storage/recovery runs report success. Independently downloaded full shard0 artifact **11000502466**, SHA256 **9ea50d3f09a9143e860ad6bf860e0483193b536a76e9a46dd52c488f7d6831c8**, and inspected the source distribution. Other old full shards were not independently audited here; old status does not substitute for the new combined gate.

Combined head **6c1ea44c701439f2b37d490c37f448e12dda122c**, tree **9e5f82de1b77e8a659c61bff607c8c2e2c7c722e**, preserves real client730780 and main04ffad5 parents. Only two runtime overlaps needed combination: daemon retains missing-server-dependency diagnostics and explicit read-only hosting; storage.git imports the neutral atomic helper while retaining group-read publication. Other client delta files reuse original Git blobs. TokenDelivery, current schema/grant checks and ownership guard were not replaced. Non-force push to the existing #171 branch succeeded.

The dev extra already includes server, so existing `.[dev]` installation is valid. uv_build intentionally includes a compatibility `pyproject.toml.orig` in its sdist; it is not a tracked leftover. Repository config reuses original blob43d0c21a, not normalized sdist TOML. No packaging exclusion was introduced.

Pending on this exact head: fresh minimal wheel install outside checkout without PYTHONPATH/server packages, four transports, helper-identity tests, hosting/token regressions and full four-shard/node-ID/conformance/build. Record proof before closing #158. Original RED history remains in CLIENT_BOUNDARY_PROGRESS.md. The combined head is not yet claimed GREEN.

## Market integration — #168 / #159

Previous market head **65b7330d248f4af96ac9ecd1f1fad3fdc6f59782**, tree **4c76f69dc2ec93fed09e4748e3aa5d846bcf35f0**. Original source base476e64f predates #167. Refreshed its current ref and comments before changing the branch. Historical RED and original focused128 GREEN remain in MARKET_BOUNDARY_PROGRESS.md and #168 comment5879093047; do not replace new-head verification with them.

Independently downloaded old full36484195530 shard0 artifact10998743543, ZIP SHA256 **7087df67dfaec8bdcf4e8ffa2e6fdc62376fa150b34d9cd2971b11406ee69028**, and inspected the source archive without running it. All **23 moved functions** were independently compared with current-main AST after only symbol/import normalization: unchanged. All remaining entrypoint handlers and other state machines also preserve their non-import AST. The internal escrow guard retains a single ledger owner and identity, not a remotely grantable capability.

This integration retains market65b733 and client6c1ea as real parents. Reuse the 19 unchanged market delta files as existing Git blobs. The only combination is market.policy: preserve the current core.query.QuerySession import and load_policy annotation while changing its delivery import to the neutral owner. No published version, SQL, signature, receipt byte, transaction or settlement state machine is rewritten. Existing client/hosting/token/core work and all test/workflow inventories remain present.

After pushing the existing market branch, run the original boundary10 plus published-contract118 regressions and full exact-node-ID/conformance/build/recovery gates on this combination. Merge #171 first and #168 second, each with an expected-head guard and exact resulting-tree verification. No final GREEN or merge is claimed by this source note; final proof belongs in the PR record. Coordination: #168 comment5888426916 and #83.

## Remaining queues

Refresh #170 Git and #155 recovery refs/comments before integrating; do not overwrite parallel work. Never count a feature-branch merge as shipping on main. Merge serially with expected-head checks and verify resulting trees.

Keep #64/#65/#68/#69/#70/#80/#84 and broad #71/#72/#85 acceptance issues open until their individual remaining criteria are met. No production permission migration, real Root/PIN/private-key operation, live funds/outbound channel, destructive backup or recovery promotion has been performed here.
