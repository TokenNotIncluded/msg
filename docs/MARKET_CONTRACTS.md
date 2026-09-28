# Market contracts: orders, delivery and disputes

This is the implementation contract for #73–75. It does not enable production
fixtures, mint funds, install arbitrators or configure an outbound mail server.
Amounts are integer MSG minor units; one MSG is 1,000,000 minor units.

## Choosing a checkout contract

`orders.buy@1` retains its original signed meaning: fund an order, then explicitly
prepare and accept a supported managed package. Previously signed requests are
not silently reinterpreted as permission for automatic settlement.
The published `orders.buy@2` keeps its digest-bound managed checkout and
optional `auto_accept` under `escrow-instant-v1`; `delivery.claim@1` remains
its separate signed acknowledgement. Its schema, receipts and decision journal
are preserved rather than reinterpreted by the new arbitration flow.

New clients select `orders.buy@3`, or use `orders.create@1` followed by
`orders.fund@1`. The latter signs the returned `order_digest`, exact total and
currency. Read `orders.contract` or `orders.get` as the buyer or seller. The
existing CLI's schema-driven invocation supports these operations; obtain each
schema from discovery rather than constructing URLs containing credentials.
All private reads use current authentication; an order ID is never a capability.

At creation, the immutable contract locks buyer, seller, quantity, listing
revision/content/terms, package revision/digest, policy/digest/grant epochs,
buyer handle and, for sealed delivery, the buyer's primary encryption subkey.
The private checkout principal is not included in seller projections. Listing
edits do not rewrite a reserved order. Funding still revalidates the current
site recipient, payment signature, balance and expiry.

Unfunded reservations count against quantity until cancelled/expired. The
serialized PostgreSQL write transaction covers quantity, balance, ledger,
transitions, receipts and the executor's idempotent result. The funding timer
starts at persisted creation; the delivery timer starts at persisted funding.
The bounded worker resolves overdue orders; GET never advances a timer.

## State and accounting

The transition table is in `msg.market.escrow.TRANSITIONS`:

```
created -> funded | cancelled
funded -> delivered | refunded | disputed
delivered -> accepted | refunded | disputed
accepted -> settled
disputed -> settled | refunded
```

Each order owns an `order_escrow` LedgerAccount, with no Subject, key or
credentials. Only the EscrowEngine's internal settlement authority can debit it.
One `order_settlements` row per order and a unique consumed decision prevent
repeated settlement, including retry with another request ID. A split appends
both ledger legs atomically. Snapshot, transition, vote, policy and settlement
facts reject SQL updates/deletes. Historical financial receipts are not rewritten.

A funded, undelivered order can be cancelled by its buyer. Missing/corrupt pinned
packages or delivery bytes are checked against server storage, not accepted on a
complainant's word. Overdue, undelivered orders refund. Unexpected programming or
infrastructure errors roll back; they are not converted into discretionary refunds.

## Delivery and claiming are different facts

For `managed_instant` file/bundle/text listings, funding verifies the complete
pinned manifest and payload digests, creates the buyer-only delivery and settles
the funds in the same transaction. Delivery remains **prepared**, not claimed.
The buyer explicitly signs `delivery.accept@2` with the delivery digest to record
claiming. Reading a page, an SMTP response and opening a Transfer do not claim it.
Automatic managed settlement is final for this escrow; it is not a promise to
refund money that has already left the escrow account.

For `sealed_manual` and `service`, funding holds the balance. The seller uploads
an immutable File (ordinary upload or Transfer), then signs `delivery.submit`
with its revision and digest. Sealed delivery additionally binds the checkout
buyer's still-active key ID and requires an age ciphertext envelope. The server
cannot prove that opaque age bytes decrypt correctly: the buyer must decrypt,
check the content and explicitly accept, or open a dispute. Services use an
immutable evidence file and the same explicit acceptance boundary. No plaintext
secret, attachment or goods content is mailed.

`delivery.get@2` has a 64 KiB total inline budget. Larger payloads advertise
`delivery.transfer_open` and bounded `delivery.part_get`. The signed Transfer
entry creates a private buyer-owned File referencing the same BlobRef, then
uses the existing TransferService. Continuations recheck the buyer's current
credential and file ACL; this does not duplicate the payload or expose the
seller's other files. Package, delivery and private evidence references are
SQL-authoritative roots for both backup and garbage collection.

## Optional checkout email

No email is required for purchase or site delivery. An existing verified email
must belong to the same buyer and match its endpoint generation. An unverified
checkout address receives only a verification challenge, never order ID, handle,
goods, pickup URL or an authentication capability. The original buyer must also
sign `orders.email_verify` with that proof. This creates an order-only endpoint;
it does not replace their account's email settings.

After verification, `orders.email_bind` can bind a current verified account
endpoint. `orders.email_disable` disables the selected notification channel and
revokes an order-only endpoint. The worker rechecks buyer/actor, delivery owner,
endpoint owner/generation/address, revocation and (for sealed goods) encryption
key ownership before sending. A stale job cannot overwrite a newly chosen or
disabled endpoint. Seller projections never contain the address.

The order notification contains exactly `order_id`, checkout `handle` and an
authenticated site `pickup` URL. Status distinguishes disabled, pending, queued,
SMTP acceptance and uncertain delivery. SMTP failure, disabled configuration or
an uncertain response does not undo site delivery or financial settlement.

## Versioned objective resolution and arbitration

`orders.dispute_open` first checks objective faults. Otherwise it creates one
private Case per order. Statements and immutable file evidence are submitted
through `orders.dispute_statement` / `orders.dispute_evidence` with `panel` or
`parties` visibility. `orders.dispute_get` and `orders.dispute_evidence_get` enforce
those roles; unauthorized and missing objects return the same error. Public
`orders.dispute_summary` exposes only random case ID, state, policy digest and
the executed outcome, not participants, order ID, votes, testimony or payloads.

The builtin `dispute-v1` has no arbitrators. It safely holds subjective cases
rather than choosing an administrator or AI. Before offering arbitrated goods,
the operator uses a physical console (preview, typed digest approval and Root
PIN) to grant registered arbitrators and publish an immutable policy:

```
msgd --config-dir /etc/msg.lmm.best market grant @arbitrator
msgd --config-dir /etc/msg.lmm.best market publish /secure/policy.json
msgd --config-dir /etc/msg.lmm.best market revoke @arbitrator
```

A policy has exactly the keys in `msg.market.policy.DEFAULT_POLICY`; copy that
shape, use a new ID, actual candidate subject IDs and explicit durations. There
are no evaluation expressions, scripts, model calls or hidden fallback members.
A grant/regrant has a new epoch; orders snapshot the active epochs at checkout.
The policy's candidate set and SHA256(order, policy, round, subject) ordering
select the panel. Buyer, seller, declared conflicts and currently invalid roles
are excluded. `orders.arbitrator_conflict` is a signed self-declaration. It does
not claim knowledge of undisclosed real-world relationships.

Panel size is bounded and quorum is a strict majority. A member signs the exact
proposal with purpose `arbitration-decision`, then submits it via
`orders.dispute_vote`. Each member has one immutable vote per round. Votes are
facts, never ledger writes. A matching quorum forms a release/refund/split
Decision; `orders.dispute_execute` or the due worker revalidates signatures,
credential ceilings, grant epochs, conflicts, round, policy and deadlines before
the EscrowEngine consumes it. Conflicting, expired, revoked or stale decisions
cannot pay again.

The explicit member-invalidation policy is **hold-no-replacement-v1**: a panel
that loses authority is held, not silently reselected. A policy may permit one
appeal with a disjoint panel selected from the same original candidate snapshot;
there is no second appeal. Old-round decisions become unusable. If no valid
quorum or eligible appeal panel exists, funds remain held and doctor reports the
held-case count. Arbitrators cannot override this by directly moving money.

## Operations and verification

Doctor's `market` check is read-only: account separation, escrow balances,
contract/policy digests, settlement conservation and consumed decisions. The
isolated `market_lifecycle` selftest creates disposable identities and a Test
Root, registers/funds a bank with 20 MSG, awards the buyer 10 MSG for a real PoP,
then buys the fixed 5 MSG bundle (`msg.lmm.best store selftest`, `hello.txt` =
`delivery-ok\n`). It checks prepared-not-claimed delivery, an in-memory SMTP sink,
verification-only email, one real signed panel refund, final bank 15/buyer 5 and
zero escrow. It never sends to a real address and cleans its namespace.

Regression files: `test_market_lifecycle.py`, `test_market_delivery.py`,
`test_market_arbitration.py`, `test_market_recovery.py`, plus the existing order,
ledger, worker, backup and transport suites. Recovery tests use a real backup and
restore; a recovery drill still blocks outbound effects and timer execution.
The migration classifies all new authoritative identity references while retaining
the fail-closed guard for unknown tables. It does not rewrite old receipts.

Existing credentials' finite ceilings do not expand merely because operations
were installed. Follow the existing local authority review/reissuance procedure
before allowing new operations. Test success is not production deployment,
real-SMTP acceptance, a production schema rehearsal or permission to mint money.

## Signed arbitration reasons

Before voting, a current panel member writes a nonempty UTF-8 `text/plain` or
`text/markdown` File (at most 64 KiB), then signs `orders.dispute_rationale@1`
with `{case_id, ref: {id, revision}}`. This pins exactly that immutable revision
as case evidence visible to both parties and the current authorized panel. It
does not share the author's file tree or disclose the reason in public summaries.

The returned `rationale_ref` and `rationale_digest` are required fields in the
canonical proposal signed with purpose `arbitration-decision`, alongside the
case/order/round/policy, allocation and expiry. Every quorum member must sign the
same reason and allocation. A later draft, another case/round, a different digest
or signature substitution cannot replace the committed reason. Both voting and
escrow execution verify the pinned bytes. Missing/corrupt reasons stop payment;
the due worker holds the case rather than inventing a replacement ruling.

Reason blobs use the existing case-evidence retention/backup roots. Restoring a
case retains its exact reason and visibility without executing it again. The
market doctor checks bindings without writing or retroactively applying current
role grants to an already-executed historical decision. This completes the new
arbitration contract in this unreleased PR; legacy `orders.buy@1` is unchanged.

## Integration with published managed checkout

The full lifecycle uses `orders.buy@3`; `orders.buy@2` keeps the already-published
package-digest/optional-auto-accept contract and `escrow-instant-v1` policy. Their
immutable decision journals are retained. Both execution paths share the same
internal OrderEscrow account check; neither path can settle the other's order.
Existing `delivery.claim@1` remains an acknowledgement, not another payment.

Version-3 contract reads and execution recheck every immutable checkout column
against its pinned contract. Funding references must identify the corresponding
buyer-to-escrow ledger entry. Settlement facts bind funding, receipt references,
settlement time and prepared-delivery identity/content, while allowing subsequent
buyer ACKs and independent email status updates. A mixed restored projection
fails closed without another ledger entry, receipt, Event or outbound job.

Mail configuration must be present **and enabled** at checkout and immediately
before invoking the sender. Turning SMTP off after enqueueing prevents the send
and marks the selected notification disabled without undoing delivery/payment.
The existing completion fences are retained for tools, Git, webhooks and mail;
expired market-mail leases also converge the selected order's notification state.
