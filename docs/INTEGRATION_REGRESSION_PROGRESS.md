# Integration regression closure — progress

Refs #78, #80, #81, #82, #83, #85. Narrow follow-up to PR #176; combines PR #177
and #178 without changing the active integration branch. No broad closure or
production operation is claimed.

## Sources and completed work

Integration base: `45470b5a46bccfc4f12ef27acccecc2f3bafce47`, tree
`e75e7cf511a89d72bfdccf5ec39567824b569275`.

The merge commit retains both independent histories:
- #177 head `769c124c83a01a648d3d30ef5d4a19fda98bedb6`: bounded transfer status
  cursors. Focused cloud run `36561282522`: 24/24, no failures/errors/skips.
- #178 head `517f7e296fa7ef504d2066980454654ed0634598`: positive integer client
  chunk-size checks. RED run `36561926956`: 14 intended failures, 9 passes;
  final focused run pending. No protocol/journal/schema changes.

## Base full-suite evidence

Run `36559785404` executed 2259 unique test node IDs in four disjoint partitions.
All four manifests and JUnit node IDs were independently compared: exact union,
no missing or duplicate cases. Four failures, no errors or skipped test cases:

- `test_legacy_routes_removed::test_public_views_require_read_effects`.
- `test_webhook_overlap::test_overlap_one_job_per_event_recipient_channel_and_pinned_revocation[endpoint]`.
- The same webhook case with `[unsubscribe]`.
- `test_webhook_overlap::test_different_recipients_and_channels_remain_distinct`.

Artifacts were independently downloaded and SHA256-verified:

| Shard | Artifact | ZIP SHA256 |
| --- | --- | --- |
| 0 | 11030251615 | e30f1fcbceeea0143e8942228db1bab8257327e3cb2cbd5d4606825b65a2b563 |
| 1 | 11030037639 | 37b09fd129f486396615d627acc83cd0696023167826ee1601ef3d3706c66468 |
| 2 | 11029802392 | 18fbb03c4531e3128ffe1e0de45602aede0532132406c1b96388ed5c934b7df2 |
| 3 | 11030251180 | e2d4990543cbf937af745f0b9429c260c024a64dee51065561a652e2171c7370 |

The full gate therefore fails; the later conformance/build stages are not
claimed complete. The same read-route failure exists on #177's shard2 run
`36561282628` (577 cases, one failure), independent of its green focused tests.

## Remaining corrections

1. `http_routes.py` resolves ordinary resource paths before checking the
   selected discovery operation's read effect. A missing path returns 404
   instead of rejecting changed effects with 405. Move the shared effect check
   before lookups and retain current request/resource authorization. New tests
   cover GET/HEAD, transaction/external effects and metadata/executor guards.
2. `communication.py::_domain_delivery_exists` uses a textual punctuation range
   for prefix matching. The real PostgreSQL overlap tests hit jobs_dedupe_key
   conflicts. Investigate collation-safe literal prefix matching while retaining
   historical scope-key recognition, exact current authorization and pinned
   endpoint/subscription generations. Never turn unique conflicts into a blanket
   swallowed error or weaken the unique constraint.

The focused workflow preserves the exact public checkout as an artifact before
running tests, so failures can be reviewed without relying on an older sdist.
It has contents:read only and never pushes commits or touches production data.

Status: combined histories, regression-first checks and workflow prepared.
Runtime read-route and webhook corrections, exact final-head focused verification,
full four-shard gate, conformance and build are pending. All project execution is
in cloud CI; local work only edits text and inspects/hashes artifacts.
