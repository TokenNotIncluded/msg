# Metadata session boundary progress

## Current integration decision

Issue #157. PR #166 is now supplemental backend-parity coverage stacked on #167,
not a competing source implementation. Coordination: #83 comment5878756363.
The canonical source is #167 commit `a85b74456be8eb20977ddd144b94483b0b21202f`,
tree `042422943d4f5a9c43e5d531797dcc3d9c92a787`.

That implementation includes the explicit QuerySession/QueryResult consumer port,
transaction-lifetime result guards and missing declared MetadataSession domain
methods. Reusing it better covers the issue's declaration/actual-dependency
acceptance condition than maintaining this branch's parallel base extraction.
All #167 source files are adopted unchanged. #166 adds only this note, its focused
workflow and the same 16 independent parity tests. The four class-name references
change from MetadataSessionBase to the canonical RelationalSession; no behavior
assertion, test, skip condition or existing upstream source is removed or weakened.
Both histories are preserved with a non-force two-parent commit.

No #167 branch, #169 executor branch, #155 recovery branch or main is overwritten.
The #167/#169 executor overlap must still converge to one implementation before
main integration. #165 has meanwhile merged to main `08c58928ee3f6c6cb350d9e9fba75d072bd78f08`;
old main/branch green results do not certify the new combined tree.

## Original red evidence, preserved

Original base main `476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7`, tree
`40ca207a3c49be347120e058e47ed328ec4e3098`.
Regression commit `7f8752e37fe5b5a8021f192b87e731caec2bbc2d`, tree
`686dbddc1bdfc52d97c127bdb7338f002cabf735`.
Run36481798533: **4 failures, 12 passes, 0 errors/skips**; all failures were
new architecture assertions, while six behaviors passed on both actual adapters.
Artifact10997260030 SHA-256 independently verified:
`50ae1ff1b71684d60e0ff133cde41c6cb81658c0855aeb9c376fff110dc27e2e`.

## Original implementation audit, preserved but superseded

The original source assembly run36482780156 verified 42 shared methods, both
complete Store classes plus FakeMetadataStore, driver execute/enqueue, PostgreSQL
SQL translation and both schemas for unchanged ASTs. Output5474eaf475e7f844244413b1f3e5109d4add4dce.
Artifact10998045352 hash:
`521d6a2f3c5d0d6b8e701302f671fad8b7d3b2641ac346b3ce4c48d282525823`.
One-shot tooling was removed from the final original tree.

Original implementation head `0ef416ec99e20604de2398622e72d7f479a84ceb`, tree
`ad1d3f4a321c25c0c3d4b6c1be8421824809e54f`.
Focused run36483460243 passed **16 new + 80 existing = 96 tests**, with
**0 failures, errors or skips**. Actual source.json and both JUnit files were read.
Artifact10997317418 SHA-256 independently verified:
`8a9cc70e9c31d19baf18050f42eda11818786d671863ded05d35b5e9ab098354`.
These results apply to that original head only, not the adopted #167 source.

## Coverage and next gate

The same tests verify isolated PostgreSQL imports, neutral-base ownership,
MetadataSession method declarations, both adapters' resource generations/path/
paging, result/event/audit/job round trips, idempotency conflicts, nested savepoint
compensation, read-only/closed/cross-task guards, unique-constraint rollback and
question-mark/percent bound-parameter handling.

The focused workflow also retains PostgreSQL locks and concurrency, security
rollback fences, ledger migrations and recovery metadata/unit suites. All project
execution stays in GitHub Actions. Final combined-head focused/full four-shard,
exact node-ID union/disjointness, conformance/build and Recovery safety outcomes
must be inspected and recorded in #166/#83 before merge; no outcome is predicted.
No production deployment, migration, Root/PIN, real funds/outbound effects, backup
destruction or recovery promotion. #65/#69/#85 umbrella acceptance remains separate.
