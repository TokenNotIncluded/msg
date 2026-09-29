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

- Regression-first head: `fa76adef7784c58483b6719057d388bf19b1526d`.
- Cloud run: `36560714465`, job `109380655492`, Python `3.15.0rc2`.
- Actual checkout: synthetic merge `b4c9965748d7e4761fc64e628921d63da11013ec`,
  tree `40302bf0a10406148bb828668d1f1e11c217fd77` (identical to the head tree).
- JUnit: 24 cases, 13 assertion failures, 11 passes, zero errors/skips.
  Eleven failures reproduce the intended cursor boundary defects. The other two
  are test assumptions about the final page: compact wire responses omit null
  `next_cursor`. Corrected to accept an absent terminal cursor, while requiring
  the exact single range and exact next cursor on every nonterminal page.
  Those two failures are not counted as product defects.
- Artifact: `11029760984`; independently checked ZIP SHA256:
  `777e5c1a4940609b0dd47f23ead2c340d6db4eeba41e116ac4d88cc886da1f00`.
- Implementation: ten lines at `TransferService.status`, after `_session` current
  authorization. Require a nonempty ASCII decimal cursor of at most 19 digits,
  value below 2**63 and no greater than known size; apply in terminal states too.
  An absent cursor still starts at zero; generated numeric cursors, leading-zero
  numeric input and the full supported int64 offset range remain unchanged.
  Malformed supplied cursors return `invalid_cursor` without echoing the input.
- No schemas, signed packets, handlers, storage APIs or publication paths changed.
- Final exact-head cloud evidence: pending. No success or closure claimed.
- No project tests were executed locally; local work only edits/inspects text and
  independently hashes/parses the downloaded evidence.

Returned numeric cursors and all six published transfer operations remain the
same. Unknown-size uploads still expose received ranges rather than inventing
missing ranges. No deployment, production data or external delivery is involved.
