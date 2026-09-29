# Issue integration notes — 2026-09-29

All project code execution, tests and builds run in GitHub Actions. Local work is source/Git/AST inspection and independent artifact parsing only. No production deployment, Root/PIN/key read, real funds/outbound delivery or restore promotion.

## Completed on main

- #156/#157: #167 merged at 261c8816; parallel flow independently audited 1894 real tests and conformance8 before closing both architecture issues. #169 superseded with its distinct regressions retained.
- #162: #172 originally merged into an old feature branch, not main. Promotion #175 fixed the target and merged as **edbfbae7648529c6b08a2e81a1a10bb349f6b50d**, tree **ebe1facca432803854d0c04aaa93519a589ff93f**. Independently inspected focused122 plus full1901 (465/496/483/457), exact node-ID union, no failure/error/skip/duplicate/missing; conformance8, measured token budget and wheel/sdist passed. Full36551495771; focused36551495692. Hashes/source identity: #175 final body and #162 comment5888054193. Original token RED/history: TOKEN_DELIVERY_BOUNDARY_PROGRESS.md.
- #161: #173 merged as **04ffad5834dcc959f5ec59696bb264c3d527990b**, tree **f377ce7dbdb53e0480552906a7d7529eee67171f**. Actual merged tree matches the verified combination. Focused hosting41 and same-head token122; full **36553079854 = 1935** (475/506/492/462), exact node-ID union, zero failure/error/skip/duplicate/missing. Conformance8, measured token budget, wheel/sdist and contracts gate passed. Original #174 independent UID/permission tests, its nine-line owner guard and real history are retained; only the duplicate product PR was closed. Exact final proof: #173 body and #161 comment5888274187. Historical RED/GREEN and deployment model: HOSTING_RUNTIME_PROGRESS.md, HOSTING_AUTHORITY_PROGRESS.md, HOSTING_RUNTIME.md.

No source test result is represented as production acceptance. Main after these merges is 04ffad5834dcc959f5ec59696bb264c3d527990b.

## Client integration — #171 / #158

Actual previous client head **730780abf1b25b91a972dc4a9cccf0cf17408728**, tree **758e86821ee31f59afbed22935207fad6cd5fb11**. Its old PR body referred to an earlier head. Full/client/architecture/storage/recovery runs report success. Independently downloaded full shard0 artifact **11000502466**, SHA256 **9ea50d3f09a9143e860ad6bf860e0483193b536a76e9a46dd52c488f7d6831c8**, and inspected the source distribution. Other old full shards were not independently audited by this execution flow; old status does not substitute for the new combined gate.

Preserve real client and current-main parents. Only two runtime overlaps need combination: daemon retains both missing-server-dependency diagnostics and explicit read-only hosting; storage.git imports the canonical neutral atomic helper while retaining every group-read publication change. Do not replace TokenDelivery, current schema/grant checks or ownership guard. Other client delta files reuse the original Git blobs; pre-existing common workflows are byte-identical.

The dev extra already includes server, so existing `.[dev]` installation is valid. uv_build intentionally includes a compatibility `pyproject.toml.orig` in its sdist; it is not a tracked leftover. Repository config must reuse original blob43d0c21a, not normalized sdist TOML. No packaging exclusion is introduced.

Pending: push combination to existing #171; run fresh minimal wheel installation outside the checkout without PYTHONPATH/server packages, four client transports, helper-identity tests, hosting/token regressions and full four-shard/node-ID/conformance/build. Record exact head/tree/run proof before marking ready or closing #158. Original RED history remains in CLIENT_BOUNDARY_PROGRESS.md. This combination is not yet claimed GREEN.

## Remaining queues

Refresh #168 market, #170 Git and #155 recovery refs/comments before integrating; do not overwrite parallel work. Never count a feature-branch merge as shipping on main. Merge serially with expected-head checks and verify resulting trees.

Keep #64/#65/#68/#69/#70/#80/#84 and broad acceptance issues open until their individual remaining criteria are met. No production permission migration, real Root/PIN/private-key operation, live funds/outbound channel, destructive backup or recovery promotion has been performed here.
