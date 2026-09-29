# Offer and purchase compatibility

Compatibility contract version 2 provides read-only Listing and Order projections
without copying authoritative records or changing published redemption semantics.
There is no startup migration and no production data rewrite.

## Entrypoints and identity

- `money.offers@1` is unchanged. `money.offers@2` also returns `listings`, projecting
  enabled, Registry-approved local offers. Both use the same validated rows.
- `store.listing_get@2` accepts `source=server_offer` and preserves the offer ID
  as `listing_id` and price revision as `listing_revision`. The default source is
  `resource`, with the same authorization as version 1. Explicit source avoids
  collisions with Resource IDs. Unavailable or stale offers are not found.
- `money.purchase_get@2` preserves the original `purchase` and also returns an
  `order`. `orders.get@2` accepts `source=legacy_purchase` for the same projection;
  its default remains `store_order`. Both require the original buyer's account
  scope and hide another buyer's purchase exactly as a missing ID.
- `orders.list@2` merges the buyer's purchases and authorized store orders into
  the same bounded private index. A `source` tag disambiguates their IDs. Sell and
  disputed filters never expose legacy purchases, whose historical read grant
  was buyer-only. Version 1 is unchanged.

Old `pur_*` IDs remain canonical for legacy records. Projections retain original
immutable offer snapshots, transaction/entitlement IDs and escrow purpose/source.
The policy is explicitly tagged `legacy-entitlement-v1`: pending means funded,
settled/refunded retain their meanings, and timestamps derive from real ledger
facts. No acceptance, Delivery or claimed fact is fabricated for a synchronous
entitlement grant. Unknown states or mismatched account/ledger/grant facts fail
closed. A snapshot is never reconstructed from the current mutable catalog.

## One protected release boundary

`market.ledger.post_escrow_release` checks the typed escrow account, original
business source ID and allowed buyer/seller recipients. Both existing order
policies and legacy entitlement settlement use it. `post_transfer` rejects
unguarded order **and purchase** escrow debits. Callers still validate their
separate signed policies and perform fulfillment/state changes in the same
transaction. Ledger receipt construction, entry keys, request IDs and historical
bytes are unchanged. Existing pending purchases remain settleable from their
frozen quotes after catalog changes. Already settled Root funds remain outside
remote cancellation authority.

Read adapters are deliberately separate from mutation record loading. A projected
purchase cannot be passed into store cancellation, acceptance or arbitration.
There is only one writable purchase record and one escrow, so enabling these
reads introduces no second settlement state machine.

## Preflight, rollback and evidence

Calling the new read contracts is a non-mutating preflight: it checks provenance
and reports incompatibility without allocating records. Disabling version 2
reads restores the former surface immediately, with no data rollback required.
Existing version 1 endpoints continue reading the same exact purchase facts.
Tests snapshot all financial/source/order/delivery/event/audit rows before and
after successful, missing and denied reads; additionally they cover quote edits,
pending/settled/refunded mappings, owner-only access, index filters, unknown
states, typed release rejection and unchanged original funding receipts.
Existing redemption failure tests verify a grant failure rolls back the shared
release and all purchase/grant facts; retry/concurrency and backup restore
coverage remain required.

The physical tables still retain their legacy names. Unmapped legacy offers
remain SQL-only; new local offers use the Resource writer described below. New
`money.redeem` versions have not been introduced. A future write-model migration must preserve historical IDs,
receipts and cached request results and retire the old writer atomically. This
read compatibility slice does not claim that physical migration is complete.


## Subsequent write-model stage

New local offers now use signed ordinary Resource/Revision authority; their SQL
rows are verified compatibility projections. Existing unmapped offers remain on
their old writer. See [OFFER_RESOURCE_MIGRATION.md](OFFER_RESOURCE_MIGRATION.md)
for the staged implementation, unchanged redemption semantics and remaining
explicit adoption/Purchase migration work.
