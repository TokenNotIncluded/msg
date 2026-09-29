# Offer and purchase compatibility boundary

This audit uses the market integration at `65b7330`. It does **not** claim
ServerOffer → Listing or Purchase → Order convergence is implemented.

## Current authoritative records

- `plugins/offers.py` reads `server_offers` for `money.offers@1` and
  `money.redeem@1/@2`. `admin/money.py:apply_offer` alone changes local offers,
  retaining the Root signature/audit boundary. Offers are not Resource revisions.
- Redemption freezes the offer in `money_purchases.offer_snapshot`, allocates a
  `purchase_escrow` LedgerAccount, and posts named `purchase_fund` and
  `purchase_final` legs through the shared `_post_transfer` function. It does not
  allocate a `store_orders` row or a `store_deliveries` row.
- `money.redeem@1` completes synchronously; @2 may defer. Existing pending
  purchases settle against their frozen quote even after catalog edits or
  disabling. They can refund only while funds remain in escrow. Settled funds
  belong to Root and `money.purchase_cancel` rejects settled purchases.
- `market/order_records.py:read_order` reads only `store_orders`. It is also used
  by mutation paths, so adding a legacy fallback there would accidentally expose
  old purchases to settlement/delivery/arbitration code written for store orders.
- `resource_entitlements` retains an FK to `server_offers`; purchase escrow
  validation and backup restore also rely on the original purchase source ID.

## Safe compatibility gate

Old enabled catalog rows now pass the same `validate_local_offer` constraints as
new console-created offers before advertisement or a *new* redemption. An invalid
unit, quantity range or duration is invisible and cannot fund an escrow. This is
a read-time filter, not data repair. It does not change the signed snapshot of an
existing purchase or its ability to settle against that snapshot.

`tests/test_offer_compatibility_boundary.py` exercises those cases against the
normal PostgreSQL fixture, verifies no ledger/account/purchase/entitlement
changes for rejection, and verifies the original funding receipt and locked
snapshot survive a later catalog edit. Existing redemption/backup tests remain
required regression coverage.

## Required next migration contract

The design requires a compatible read or explicit migration; neither exists yet.
The implementation must define these mappings before migrating any live rows:

1. Preserve `pur_*`/offer IDs and their references. Define whether the original
   IDs are the canonical compatibility identities or aliases for new `ord_*`
   and Resource IDs; aliases must not silently rewrite old signed requests or
   escrow source IDs. Reject collisions, unknown states and incomplete sources.
2. Define an explicit **legacy entitlement policy**, separate from new store
   signature-acceptance policy. Legacy settled purchases contain no buyer
   acceptance fact. A migrated/read-projected order must never synthesize one,
   nor fabricate a Delivery claim or signature from a grant.
3. Define how immutable offer snapshots become revision/terms references. Current
   offers are mutable rows and a historical snapshot may differ from them. A
   migration cannot substitute the current price revision or recompute historical
   payment/receipt digests as though they were original signed facts.
4. Preserve owner-only legacy reads and Root/local-only mutations. A generic
   seller view or public Listing index must not widen purchase access or give
   ordinary sellers an entitlement issuance path.
5. Bind each migrated escrow to exactly one authoritative business record and
   settlement path. Test pending cancellation, settlement, concurrent retry,
   recovery and rollback before retiring `purchase_escrow` references. A view
   alone must not make both purchase and store mutation handlers spend it.
6. Store and verify migration provenance/checkpoints and provide rollback that
   retains exact receipt bytes, ledger sequence, request replay results and
   entitlement IDs. No startup-time silent financial rewrite.

These are implementation obligations for the next slice, not permission to
change published request semantics. The present patch changes no DDL, request
schema, historical record, financial state machine or production data.
