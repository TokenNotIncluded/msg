# Offer/Order write-model convergence

Baseline: central `ce497d6`. The current design requires a Listing authority for
server offers, one Order model for purchases, unchanged published redemption
versions, and no silent changes to historical IDs, signatures or ledger receipts.
The preceding v2 read adapters are compatibility projections, not a migration.

## Small reviewable stages

1. **New-offer Resource writer (implemented).** Newly created local offers use
   ordinary `listing` Resources under `/store/` with Root-signed immutable
   Revisions. Their unchanged offer IDs are Resource IDs. `server_offers` is an
   atomic compatibility projection for these records, identified by the typed
   `server_offer_resources` mapping. Local set/disable updates append a revision;
   public reads/redemption verify the projection against its signed authority.
   An existing unmapped offer stays legacy, even on edit. Nothing imports it
   implicitly. This stage neither migrates Purchases nor changes write contracts.
2. **Explicit existing-offer adoption (implemented).** The local dry-run/import command uses
   an exact expected legacy snapshot, ID/name collision checks, explicit provenance
   and an auditable approval plan. Import must preserve each offer ID and all referenced
   purchase snapshots, entitlements and ledger bytes. Reject unknown providers
   and inconsistent records. No database-startup import. Existing-row edits stay
   on their original writer until explicit adoption.
3. **New entitlement Order writer (synchronous part implemented).** The new redemption version has an
   immutable entitlement policy represented in the shared Order contract;
   deterministic grant/payment commit together and Root settlement remains
   non-remotely-refundable. Later deferred fulfillment must use the same typed escrow. Keep
   legacy Purchase readers/writers until explicit pending-order adoption and
   rollback are verified. Never synthesize acceptance or Delivery from old grants.
4. **Retire legacy writers only after evidence.** Verify old request replay,
   concurrency, backups, restore and rollback against signed historical fixtures;
   retain original IDs/receipt references and remove the obsolete writable path
   only when it cannot spend the same escrow a second time.

## Stage 1 authority and behavior

The existing local-console/PIN/Root signer boundary is unchanged. Ordinary users
cannot reprice a Root-owned listing, manufacture server entitlements, or route an
entitlement listing through generic store checkout. `money.redeem@1/@2` retain
their exact arguments and resulting purchase/ledger semantics. The Resource body
has normal sale fields plus the typed `server_offer` snapshot and model version.
Its policy is explicitly the old entitlement policy, not buyer acceptance.

Each successful set creates a fresh signed Revision whose ID is the existing
`price_revision` field. Disable creates another Revision with `state=paused` but
retains the quote ID, matching the old disable behavior. Historical revisions and
pending purchase snapshots remain readable and immutable. Reads do not adopt old
rows or repair mismatching projections; a mismatch rejects new redemption before
funding and cannot be used as input to the local writer. The public compatibility
view also rejects a private or inactive authoritative Resource/ancestor instead
of bypassing its visibility through the old offer endpoint.

Mapping, Resource pointer, Revision row, compatibility projection and local audit
are written in the caller's single metadata transaction. The content store uses
its normal durable-before-pointer publishing and rollback unpin mechanism;
failed writes cannot expose a committed Resource or financial side effect.

Schema startup creates only an empty mapping table; it does not convert data.
Fresh production catalogs remain empty. The mapping table is included in normal
PostgreSQL dumps and therefore follows the same backup/restore transaction as
Resources, revisions and legacy references.

## Verification and remaining boundary

Tests cover signed source verification, immutable old prices and deferred
settlement, ordinary Listing reads, disable semantics, projection tampering,
remote seller/checkout rejection, rollback after content publication, retry, and
no implicit adoption of old rows. Existing offer/redemption/admin/backup tests
remain required.

Only newly created offers and their subsequent edits use the unified Resource
writer in this stage. Existing Offer adoption, Purchase → Order writer migration,
and a new redemption contract remain separate stages. Do not label those complete
based on this change or the prior read projections.

## Stage 2: explicit old-offer import

`msgd money offer import OFFER_ID --dry-run` is a read-only local preflight and
needs no Root PIN. `msgd money offer import OFFER_ID` prints a newly generated
complete plan, requires its exact `CONFIRM MONEY sha256:...` string, then unlocks
Root. A dry-run never authorizes a later run; the operator approves the actual
plan being applied.

The plan fixes the exact old quote, target IDs, purchase/grant counts and a digest
of original serialized Purchase rows, entitlement rows and ledger rows (including
receipt bytes). Before publishing, the write transaction repeats consistency and
collision checks and compares the complete plan. Any new purchase, cancellation,
settlement or quote edit since approval makes it stale. Inconsistent grants or
ledger references, unsupported providers, existing Resource IDs/names and an
already imported offer are rejected.

Import creates a fresh signed Revision with a fresh revision ID and explicit
source digest referencing the approved plan. It preserves the old `offer_id` and
`price_revision` verbatim; the new revision is an import fact, not a claim that a
historical quote was originally a Resource revision. Existing snapshots, grants,
accounts and ledger bytes are untouched. The signed Root audit contains the plan
and approval digest. Failure during publication rolls back Resource, mapping,
Revision and audit in the same transaction; the unchanged plan can be retried.
No automatic startup adoption or remote import operation is registered.

## Stage 3: explicit synchronous entitlement Order writer

`money.redeem@3` is a synchronous Order contract and the dedicated CLI default. It requires an
already adopted/new Resource-backed offer and these signed fields:
`offer_id`, `quantity`, `currency_id`, `price_revision`, `listing_revision`,
`offer_snapshot_digest`, `total_price_minor`, and
`settlement_policy="deterministic-entitlement-v1"`.

The snapshot digest covers the exact public offer (resource/entitlement kind,
unit, unit price, bounds, duration and provider version). The independently bound
Listing revision fixes the Root-signed terms. The full original signed request
is retained and verified against the buyer's recorded signing credential during
contract validation; recomputing a database digest cannot forge consent.
Revocation still blocks new requests and cached-result replay, while the old
signature remains valid evidence when the owner reads through a current key.

This uses the existing `store_orders`, `order_contracts`, `order_transitions`,
`order_settlements`, protected OrderEscrow, funding function and settlement
function. It creates no `money_purchases` row and no parallel purchase state
machine. Policy version 5 allows **only this explicitly consented deterministic
entitlement policy** to go funded → settled: the ResourceEntitlement grant and
release to Root must succeed in the same transaction. It creates neither Delivery
nor accepted/claimed facts. This follows the design's same-transaction
deterministic-entitlement rule; ordinary `orders.buy@4` still requires separate
signed buyer acceptance and retains all previous checks.

A grant failure after ledger release rolls back funding, release, Order, grant
and events together. Repeating the same signed request returns the original
result. Competing spends use the same protected ledger transaction. Native order
reads and payment receipts validate the exact grant and settlement; remote
cancellation cannot refund an already settled Root payment. PostgreSQL order
quantity is widened from INTEGER to BIGINT to preserve byte quantities above
2 GiB without changing any existing values or signed facts.

Published `money.redeem@1/@2` keep their original writers and response shapes;
existing pending Purchases are untouched and still settle/cancel independently.
Version 3 deliberately has no `defer` flag: asynchronously fulfilled new orders
and explicit adoption of historical pending Purchases remain later work, not
features inferred from this synchronous contract. All production migrations and
financial actions remain manual and local; tests use isolated fixtures only.

The dedicated `msg money redeem` fetches `store.listing_get@2` from the ordinary
Resource branch, binds its actual revision and complete quote digest, and signs
@3 with explicit synchronous-settlement policy. Supplied price/revision/digest
fields must match exactly; a stale pinned intent is rejected. Unimported offers
are rejected by default; `--contract-version 1` or `2` explicitly selects the
legacy writer, including `defer` only under @2. Reusing a request ID after the
quote changes fails closed; it never silently replays a differently priced buy.
The isolated official selftest retains all 20 → 10 → 5 PoP/store acceptance
checkpoints, then exercises a separate one-minor-unit deterministic grant using
a real @3 buyer signature, replay and supply checks.

This stage converges new redemptions onto the existing native Order writer. Stage 4 below gives new default Orders ordinary Resource/Revision authority.
Historical Purchase adoption remains separate. Compatibility reads are not
evidence of migration.

## Stage 4: controlled ordinary Order Resources

Design lines 56/58 define the shared Resource/Revision structure and reserve
controlled facts to dedicated operations; lines 64/118 require Order convergence
and unchanged old signed policies; lines 135/193 prohibit index-derived financial
authority and silent historical rewriting. Issue #85 repeats Resource/Revision
reuse and the single protected ledger; #71 requires preserved #102 contracts and
atomic, recoverable fulfillment.

New `orders.create@2`, `orders.buy@4` and `money.redeem@3` contracts carry an
explicit `resource_model=1` marker. Their ordinary private `order` Resource keeps
the same `ord_` ID at `/@buyer/orders/<id>` under a controlled `order_collection`.
Its immutable JSON Revision contains the original locked contract, core Order
state, funding/settlement times and receipt references, settlement fact, and
transition facts. New v4 contracts also preserve the original creation request
verbatim. Revision manifests are signed by the service receipt signer as
**operation facts**, not falsely represented as buyer-signed content. Original
request signatures and financial receipt bytes are unchanged.

`store_orders` and `order_contracts` are validated query projections for marked
orders: every record/contract read compares their canonical document digest to
the current immutable Revision metadata. Dedicated operations additionally read
and verify source bytes and the Revision signature before acting. Inventory
checks validate existing Resource orders before using SQL quantity/state indexes.
Projection drift, deleted source, missing content or invalid signatures fail
closed; no startup repair, adoption or automatic re-signing exists. Ledger,
Delivery, arbitration evidence and notification jobs remain their respective
financial/delivery/evidence facts, not parallel Order state machines. Notification
endpoint changes are intentionally outside the Order content snapshot.

A dedicated operation starts from a verified source, validates the existing
versioned business rules, then appends its final committed Order Revision in the
same transaction as the SQL projection and ledger changes. Internal transitions
within one operation are preserved as transition facts; they are not separate
commits. No-op/replay creates no new Revision. Buyer acceptance, objective refunds,
and arbitration retain their existing semantics. The timeout and internal
arbitration entrypoints use the same boundary; Resource-backed transitions
outside that boundary are rejected. Any source publication failure rolls back
Order, Delivery, ledger, grants, events and the revision pointer together.

Generic content/file creation, write, lifecycle, ownership, group, permission and
move operations cannot edit the controlled types. The buyer can read ordinary
Resource bytes; sellers still use the existing role-trimmed market view. Source
refs are returned as `data.order_resources`; they do not add generic descendant
read rights to existing account-scoped credentials or change replay authority.

Existing orders without the marker retain their published compatibility writer
and policies, including old @1/@2/@3 and previously created @4/@3-redemption
records. No historical request, ID, receipt, pending Purchase or Order is adopted
on read or startup. Explicit historical Order/Purchase adoption is intentionally
not claimed; the design permits this compatibility boundary. New legacy-version
requests continue their exact published writer and response shape.
