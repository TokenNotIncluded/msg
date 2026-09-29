# Offer/Order write-model convergence

Baseline: central `ce497d6`. The current design requires a Listing authority for
server offers, one Order model for purchases, unchanged published redemption
versions, and no silent changes to historical IDs, signatures or ledger receipts.
The preceding v2 read adapters are compatibility projections, not a migration.

## Small reviewable stages

1. **New-offer Resource writer (this change).** Newly created local offers use
   ordinary `listing` Resources under `/store/` with Root-signed immutable
   Revisions. Their unchanged offer IDs are Resource IDs. `server_offers` is an
   atomic compatibility projection for these records, identified by the typed
   `server_offer_resources` mapping. Local set/disable updates append a revision;
   public reads/redemption verify the projection against its signed authority.
   An existing unmapped offer stays legacy, even on edit. Nothing imports it
   implicitly. This stage neither migrates Purchases nor changes write contracts.
2. **Explicit existing-offer adoption.** Add a local dry-run/import command with
   exact expected legacy snapshot, ID/name collision checks, explicit provenance
   and rollback manifest. Import must preserve each offer ID and all referenced
   purchase snapshots, entitlements and ledger bytes. Reject unknown providers
   and inconsistent records. No database-startup import. Existing-row edits stay
   on their original writer until explicit adoption.
3. **New entitlement Order writer.** Publish a new redemption version whose
   immutable entitlement policy is represented in the shared Order contract;
   deterministic grant/payment commit together, deferred fulfillment holds the
   same typed escrow, and Root settlement remains non-remotely-refundable. Keep
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
