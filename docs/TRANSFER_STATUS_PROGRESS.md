# Transfer status continuation — progress

Scope: Refs #78, #80, #83, #85. This is a narrow correction on top of PR #176,
not a second transfer implementation or a claim of complete storage acceptance.

## Source and ownership

- Base commit: `45470b5a46bccfc4f12ef27acccecc2f3bafce47`.
- Base tree: `e75e7cf511a89d72bfdccf5ec39567824b569275`.
- Independent branch: `fix/transfer-status-cursors-20260929`.
- Changes: the shared `TransferService.status` boundary, a regression file and
  a focused GitHub Actions workflow. Do not overwrite the active integration
  branch or change its schemas, migration code or CI completeness gate.

## Reproducing the gap

Known-size status currently calls `int(cursor)` in missing-range storage;
unknown-size status calls it in the projection. Malformed values can therefore
become internal errors, while negative unknown-size offsets and malformed
sealed/download cursors are not consistently rejected.

The new regressions exercise real Ed25519-authenticated operations against
PostgreSQL, HTTP/PathGET/GraphQL/MCP clients and the fixed transfer endpoint.
They compare full database row values and block publication/outbound interfaces
around reads. They also preserve continuation order, int64 bounds, owner-first
checks and revoked-credential rejection.

## Evidence

- Regression-first commit: tests and workflow only; implementation unchanged.
- Cloud failure evidence: pending. No project tests were executed locally.
- Implementation/final cloud evidence: pending; no success or closure claimed.

Run the focused workflow, record its exact source/tree, JUnit result and artifact
hash, then apply bounded decimal cursor validation after current authorization.
Returned numeric cursors and all six published transfer operations remain the
same. Unknown-size uploads still expose received ranges rather than inventing
missing ranges. No deployment, production data or external delivery is involved.
