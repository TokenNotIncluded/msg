# Issues #75–77: local implementation and acceptance boundary

Repository: `TokenNotIncluded/msg.lmm.best`
Upstream base: `ad256d037ec4febbee95f03b7044f13b9eebb485`
This change is a **partial implementation**, not authorization to close all three issues.
No remote push, pull request, merge, deployment or issue closure was performed.

## #75 — Objective escrow decisions (partial)

`msg.market.escrow.EscrowEngine` is now the single debit path used by
`orders.cancel@1` and `delivery.accept@1`. The installed `escrow-v1` / `dispute-v1`
policy preserves the existing two explicit operations: buyer cancellation before
any delivery, and buyer acceptance after verification of the immutable package
and delivery digest. An unknown policy is rejected before checkout funding.
A disputed order cannot be accepted or cancelled through these operations.

Every successful payout atomically appends an Ed25519-signed decision, its source
request proof and the resulting ledger transaction ID to `order_escrow_decisions`.
The decision binds the order snapshot, evidence digest, recipient, amount,
currency, request, policy version/digest and expiry. Order ID uniqueness prevents
multiple decision records. Database triggers prohibit updating/deleting decisions.
Ledger posting, delivery state, order state, decision, Event and request result
remain in the executor transaction. A failed journal append rolls back the debit.
The decision is produced from current facts, not accepted as a caller-supplied
arbitrary payment instruction. It is signed by the service receipt key, **not by
an arbitrator**. Old settlements are not backfilled with invented decisions.

Not implemented here: ArbitrationCase and role-scoped evidence, fixed candidate
pool, conflict exclusion, panel/quorum, arbitrator-signed refund/release/split,
appeal, arbitrator role/key revocation, objective delivery deadlines, public case
summaries, or their complete doctor/selftest/backup acceptance matrices. Automatic
timeouts and splits are explicitly disabled. This is not a complete arbitration
implementation and #75 must remain open.

## #76 — Current share-source checks (partial acceptance)

`share_target_error` centralizes the same resource boundary for grant issuance,
source validation, ordinary shared reads and ShareLinks. Containers, inactive
ancestors, protected platform rules, tools/credentials/certificates, SOUL/AGENTS,
Todos, Legacy, DMs and hosting previews cannot be made readable by these sources.

ShareGrant@2 remains **read-only with empty constraints**. Unsupported or malformed
stored operations/constraints are rejected instead of treating `read` membership
as sufficient. Sources are checked for current expiry, future creation time,
active group resources and membership. The bounded parent chain is checked for
cycles, owner changes, reshare permission and expiry attenuation, including after
restoration of inconsistent facts. Rejecting one source does not veto a separate
valid source. Single-item sharing does not grant parent-directory enumeration.

Coverage includes existing owner/group/reshare/revoke/ShareLink tests and added
restored-fact/target-boundary regressions. The exhaustive cross-product across
HTTP, GraphQL, MCP, CLI, GET Path, Sync, cache/attachment projections and backup
restoration has not been completed. Do not infer that complete matrix acceptance
or PostgreSQL deployment verification follows from the focused tests.

## #77 — Personal revision proofs and honor display (partial acceptance)

New `identity.personal_put@2` and `identity.note_put@2` require `resource_id`,
`revision_id`, `content_created_at` and `content_signature`, in addition to their
usual content fields. The signature uses the existing `revision` purpose and the
canonical Revision manifest (excluding `signature` and `manifest_digest`). Existing
content additionally requires the current `expected_revision` and generation.
Each immutable Revision carries its own verified signature. Failed verification
rolls back publication; replacing content does not rewrite prior revisions.
The complete source example is in `tests/test_personal_content_signatures.py`.

Version 1 remains backward compatible and accurately retains **request-purpose**
proofs; it is not silently relabeled as independently signed content.
Personal documents still default to private. No read, DM, tool result or model
inference is converted automatically into Notes, SOUL or AGENTS.

`achievement.pin@1`, `achievement.unpin@1` and `achievement.reorder@1` require
owner signatures and current credential scope. Pins are separate rows, never
edits to signed grants or security certificates. Maximum 32 pins; reorder must
contain exactly the current visible pin set. Revoked grants disappear from pin
projections without read-time writes. `achievement.list@1` adds the ordered
`pinned_grant_ids` field, without returning private challenge evidence. Existing
R1–R5 tests still run; no declarations were made on a real user's behalf.

Secret screening detects only the explicitly listed PEM/age/common-token formats
and labeled long secret values. The English override-expression heuristic is a
convenience filter, not natural-language understanding. Arbitrary/obfuscated
secrets or instruction semantics cannot be guaranteed detectable. Crucially,
AGENTS prose is never read as an authorization source: even an unrecognized
instruction to ignore permissions cannot change server access rules.

Not completed here: independent Revision signatures for every personal/Legacy
operation, the full CLI/TUI recovery/display matrix, and every honor strategy and
custodial final-signature variant requested by #77. Legacy remains an inert signed
directive; there is no automatic loss-of-contact execution or implicit delegation.

## Compatibility and verification

The new operations extend the finite `identity.basic` operation list. Previously
signed root/online-CA policies and credential ceilings are **not auto-expanded**.
An existing deployment must explicitly review and authorize any necessary policy
update; do not silently rewrite signed certificates to enable the new operations.

Production storage still defaults to PostgreSQL. Deferring the PostgreSQL/Valkey
imports until actual default-storage construction only permits the existing
injected metadata-store path to be exercised without those optional runtimes;
it does not introduce an automatic SQLite fallback.

Local verification used Python 3.13.5, real Ed25519, Git content storage and
serialized SQLite transactions. A disposable, explicitly labeled test-only
adapter supplied money tables, a SQLite subject-account trigger and a serialized
sequence equivalent. It did not emulate PostgreSQL advisory locks, migrations,
sequence rollback behavior, multi-host operation or the production Python 3.15
runtime. The adapter and replacement fixture are **not in the production patch**.
No original test assertions were weakened. One existing refund-failure injection
was moved from the former orders helper to the new escrow helper.

Machine-readable results and test logs are shipped separately in the delivery
archive. They are local results, not the repository's GitHub Actions results.
PostgreSQL/Python 3.15, full conformance, doctor/selftest, distribution build and
production deployment remain verification gates before merging or closing issues.

Pre-deployment review must also identify any uncompleted historical order carrying
an unknown escrow/dispute-policy label. Such orders fail closed in this patch;
they require an explicitly approved reconciliation/migration plan rather than
silently guessing a policy or rewriting signed history. The focused local tests
do not establish that a production database has no such orders.
