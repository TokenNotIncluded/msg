# Integration regression closure — progress

Refs #78, #80, #81, #82, #83, #85. Integration entry: PR #176.
Latest narrow follow-up: PR #180. Live verification, exact final head and merge
state: #176 comment 5890775291 and #180. This note is a timestamped handoff,
not a second completion authority or a claim of production acceptance.

## 2026-09-29 13:05Z: current repair

Input integration head `de3cd872b71e062d6141916b11ea0bdfa28adca6`.
Actual previous cloud checkout `9b727c16b893959e358811e543233df1608abb7b`,
tree `4936af96eb805c4f90aad002008376425a2effa4`.

Independently downloaded all four artifacts of run **36564681470**, checked
provider SHA256, each JUnit msg.nodeid against selected/all, common collection,
exact disjoint union and no duplicates: **2407 unique cases, 6 failures,
0 errors, 0 skipped**. Four effect-order failures and two signed descending
read_query@1/@3 failures; the earlier webhook-overlap failures are absent.

| Shard | Artifact | ZIP SHA256 |
| --- | --- | --- |
| 0 | 11031896647 | fd8c2110ab4240246af4d3b323b5676b7458af5016b6ed18c940010772a691e0 |
| 1 | 11031762212 | 95bf9181ea1c39acd3d9ea62edc0d194104e116d93a699b38c01a53d704f0dbb |
| 2 | 11031389353 | 7a00d14bea7a34c8d1439f936e8d78813c9d76f973fec0e3e518e5ffd4c52a1d |
| 3 | 11031559533 | d7c9c081408ad09b01be264e3e3ac6df4d84f020a4bab4d9f2d2f3eca5ddc5df |

### Submitted implementation

Source commit `12e829a6a7b90c279e9b15cd74cece63978ca387`, tree
`07449e7ae96399ebcbcd1ae33a53de3d9ab5d8f5`, in #180 based on #176:

- Remove artificial initial empty-string/U+FFFF keyset endpoints. No seek on
  the first page; later scans/cursors keep actual SQL keys. Ordering, principal,
  query/snapshot binding, authorization and budgets are unchanged.
- Retain the real runtime-generation SQL read in the effect test. Precisely
  forbid every other SQL query and executor call, check each request actually
  reads generation, and also test stale generations fail closed. Recovery
  middleware is not weakened, bypassed or mocked to succeed.
- Add list/search/read_query@1/@2/@3 checks for all three sort keys in both
  directions, Unicode names, exact database order, nonoverlapping continuation
  and empty results. Existing signed four-transport tests remain unchanged.

Source-only preparation run 36571871534 published just one reviewed source
blob. Artifact11035120921 ZIP SHA256
`02f5421eb68c126198dd1fe0f6936918f339470d9350fa201846a5ed0434d47d` was checked
independently, including byte-for-byte source equality. Source blob
`985c0c222220bfc23b0e28b4e54871bef5e7885d`; SHA256
`2cc2b605e6590d61e0f65e63f613f75e1488b2d40c77224d1b77b4012694ffc0`.
The auxiliary workflow neither ran project code nor updated a branch and is
absent from the product tree.

### Verification in progress; no GREEN claim yet

Initial full run 36572191345 and integration run 36572191005 were triggered.
Focused run 36572191224 failed in the newly added collation diagnostic before
pytest: lc_collate is not a supported current_setting parameter. The follow-up
reads pg_database.datcollate for current_database(), as documented in the
PostgreSQL 16 system catalog, and fixes the health probe's database name.
Runtime source and test assertions are unchanged by that CI correction.
New runs must identify their actual final head/tree; older partial or successful
runs do not satisfy the final combined gate.

## Preserved earlier history

Earlier integration base `45470b5a46bccfc4f12ef27acccecc2f3bafce47`, tree
`e75e7cf511a89d72bfdccf5ec39567824b569275`. PR #179 retained both histories
and was merged into #176 as de3cd872; no duplicate implementations are needed:

- #177 head `769c124c83a01a648d3d30ef5d4a19fda98bedb6`: bounded transfer
  status cursors. Historical focused run 36561282522: 24/24, zero failures,
  errors or skips. See its original evidence note for full provenance.
- #178 head `517f7e296fa7ef504d2066980454654ed0634598`: positive integer
  client chunk sizes. Historical RED36561926956: 14 intended failures,
  9 passes. Original transfer/client evidence remains authoritative for those
  narrow changes, not current integrated acceptance.

Earlier full run36559785404 had 2259 unique cases, four failures, no errors or
skips: one read-effect-before-lookup case and three webhook-overlap cases.
Exact manifests/JUnit union and provider hashes were independently checked:

| Shard | Artifact | ZIP SHA256 |
| --- | --- | --- |
| 0 | 11030251615 | e30f1fcbceeea0143e8942228db1bab8257327e3cb2cbd5d4606825b65a2b563 |
| 1 | 11030037639 | 37b09fd129f486396615d627acc83cd0696023167826ee1601ef3d3706c66468 |
| 2 | 11029802392 | 18fbb03c4531e3128ffe1e0de45602aede0532132406c1b96388ed5c934b7df2 |
| 3 | 11030251180 | e2d4990543cbf937af745f0b9429c260c024a64dee51065561a652e2171c7370 |

That historical run was RED, with later conformance/build not claimed complete.
#179 moved read-effect checks before resource lookups and corrected literal
webhook prefix matching without removing unique constraints or swallowing
conflicts. The current combined six failures above supersede that old triage.

## Unchanged completion and execution boundaries

Require final focused suites, all four shards, exact disjoint node-ID coverage,
conformance/actual tokenizer, wheel/sdist checks and applicable specialist gates.
No removed tests, skips, relaxed authentication or substituted old-head greens.
All project execution happens in cloud CI; local work only edits source text
and independently reads/hashes artifacts.

#78/#80/#81/#82/#83/#85 broad acceptance and #64/#65/#68–70/#84 field evidence
remain open. No production deployment, Root/PIN access, money movement, real
outbound delivery, backup/key destruction or recovery promotion is performed.
