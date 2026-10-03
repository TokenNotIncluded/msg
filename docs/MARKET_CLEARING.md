# Clearing, bounty and order contracts

This page records the #71–#73 implementation boundary. It is not production deployment approval. Currency is `primary`, scale **6**; `MSG` is a display name, not a fiat peg. Initialization creates zero supply, no bank, no server offer and no listing.

## Local central bank

```sh
msgd money mint 20
msgd money bank fund @bank-test 20
msgd money bank remove @bank-test
```

`bank fund` resolves the stable subject, displays **two** separately digest-bound previews (grant BankRole; transfer existing Root funds), then requests the hidden PIN. Both confirmations precede unlock. The role, transfer and two audit records commit together; rejection, changed preview state or insufficient funds leaves neither a new role nor a transfer. Removing BankRole does not confiscate a balance. Mint/burn, ordinary Root transfer and offer changes retain their own confirmation.

The command defaults to the existing OS-root physical-console gate. Only mint,
burn, transfer, bank-add, bank-remove and bank-fund accept an explicit
`--allow-ssh` with the OS-root SSH-terminal check. They still require interactive
confirmation and Root PIN entry. Other money actions and market administration
remain physical-console-only. Network adapters, `--yes`, PIN arguments,
environment variables and piped confirmation are not substitutes.
`_confirmed_money` is an internal use-case, not a remotely registered operation.
Its isolated selftest replaces only console IO and the disposable Test Root
unlock; production performs the applicable administrator check before loading it.

`ClearingPolicy` v2 is a pure integer decision with no bank/CA/priority exception. Local issuance and ordinary settlement share posting invariants. Each signed ledger receipt includes actor, request ID, named posting leg, policy version/digest and sequence. `(actor, request_id, entry_key)` is unique: one purchase request may fund and release escrow, but may not post either leg twice. Historical v1 receipts remain unchanged. Corrections append refunds; ledger and order transition history reject UPDATE/DELETE.

## Real purchasable capacity

Only Registry `website` is currently marked purchasable. The installed provider is **`hosting_bytes_v1`**, unit `byte`, provider version 1. It increases the owner's aggregate active website payload capacity, not file storage, identity permissions, CA capabilities, account priority or a right to edit someone else's website.

```toml
[hosting]
base_capacity_bytes = 10485760
```

The default base is 10 MiB; the validated range and total hard limit are 1 byte through 1 TiB. Preview, deploy, activation, website restoration and ownership changes check the limit after ordinary authorization. Files are counted by logical size, including duplicate deployments. Expired grants stop contributing to future deployment capacity; already published content is not deleted merely because a grant expired. Existing deployments above the newly explicit base require a deliberate capacity/configuration decision before another deployment.

Root `money offer set` accepts only the installed resource/provider/unit tuple, a positive integer price, bounded quantities and an optional positive duration (maximum ten years). Merely registering a ResourceType does not create an offer. Unsupported tuples and an empty catalog fail closed.

`money.redeem` requires `offer_id`, exact `price_revision`, quantity and `currency_id=primary`. It locks the full quote. The ordinary path creates the purchase, transfers funds to a keyless purchase escrow, grants a `ResourceEntitlement` and settles to Root treasury in one transaction. The irreversible delivery point is the **entitlement and final ledger commit**, not the creation of the quote. A provider failure rolls back all business facts.

`money.redeem@2` adds `defer=true`, which creates a pending reservation with a fixed **900-second** expiry. Only its buyer may explicitly call `money.purchase_settle` or `money.purchase_cancel`. Settlement uses the locked quote despite a subsequent price edit or offer disable; expiry or unavailable provider refunds the exact reservation. A capacity failure leaves a pending purchase cancellable rather than charging again. Pending orders do not reserve entitlement capacity. GET does not settle, cancel, extend or expire business state. No asynchronous worker or unsolicited Root payment is implied.

## Bounty

Execution and public projections compare the accounting row with the immutable Listing terms: publisher, reward, claim limits, verifier, eligibility and expiry must agree. A mixed restore fails with `bounty_contract_mismatch` before consuming a nonce or moving money. Top-ups and pause/claim state remain separate mutable accounting facts. `doctor` performs the same comparison through a read-only content reader.

Activation first moves the publisher's budget to a keyless BountyEscrow. `signature_pop_v1` challenges bind listing, claimant, random nonce, issue/expiry times and verifier version. TTL is **300 seconds**. Both issuance and claim require the claimant's current primary IdentityKey. Rotating/revoking it invalidates pending proofs; a signature from a different key cannot redeem one.

A paid Claim, nonce consumption, escrow transfer, signed receipt, Event and minimal Inbox reference are one transaction. The publisher need not stay online. Failed proofs do not consume budget. Concurrent last-budget claims, per-subject limits, close/refund and repeated request IDs are serialized. Top-up cannot silently resume a publisher pause or a reached maximum claim count. `bounty.get` and `store.listing_get` share the same current-state projection; immutable listing revisions retain their historical values separately.

**PoP proves control of the current signing key, not humanity, one-person-one-account or Sybil resistance.**

## Order policies and failures

| Policy | Delivery mode | Funds release |
| --- | --- | --- |
| `escrow-v1` | managed_instant | Existing compatible flow: explicit buyer prepare and accept |
| `managed-instant-v1` | managed_instant | Atomic verified deposit → site delivery → policy settlement |
| `sealed-manual-v1` | sealed_manual | Seller uploads a buyer-key-bound envelope; buyer explicitly accepts |
| `service-accept-v1` | service | Seller submits completion evidence; buyer explicitly accepts |

All four use `dispute-v1`; unknown combinations are rejected. A policy name/version is part of the locked order. Existing signed listings and historical receipts are not rewritten to choose automatic settlement.

`orders.create` creates an unpaid, private quote valid for **900 seconds**. It reserves neither funds nor stock. `orders.pay` requires that quote's digest, exact total and currency; current stock/availability are checked at payment, while later seller edits do not replace the locked price/terms/package. `orders.buy` combines quote and payment. Competing buyers cannot consume the same last quantity. Opaque random IDs are not authorization; unauthorized and missing orders have the same error.

The transition table is `created → funded → delivered → accepted → settled`, plus `cancelled`, `refunded` and `disputed`. Every transition is append-only history inside the transaction. `accepted` is never left half-committed. System escrows have no Subject, keys, profile or ordinary transfer capability. Ordinary/bank/arbitrator identities cannot supply an arbitrary escrow debit, recipient or split.

Automatic `accepted` records **policy consent in the signed order**, not a fabricated inspection signature. Automatic settlement leaves Delivery `prepared`, with no `claimed_at`. A later signed `delivery.claim` records actual buyer acknowledgement without paying again. Read-only download and HTTP/SMTP success never set `claimed`. Legacy/manual `delivery.accept` acknowledges receipt and settles escrow together.

Funded orders have a **24-hour delivery deadline**. Buyer `orders.resolve` derives evidence from server facts: missing immutable deposit/content, digest/size mismatch, missing recorded delivery, or an expired undelivered order. It can cancel an expired unpaid quote or refund an unfulfilled escrow. Temporary storage/network errors do not prove merchant fault. A healthy delivered service cannot be automatically refunded merely because the buyer dislikes it. Buyer `orders.dispute` freezes a delivered order; seller `orders.refund` can return the full escrow only to that buyer. No ordinary subject can invent a reason, amount, recipient or arbitration decision for the resolver.

For sealed delivery the current buyer EncryptionSubkey is checked at submission; the immutable envelope and key metadata are fixed. The server verifies an age envelope and its digest, **not** successful decryption or the quality of the encrypted goods. The buyer must decrypt and inspect before acceptance. Service delivery similarly records evidence, not a quality guarantee. Payloads remain bounded to the existing 1 MiB inline delivery path; large Transfer delivery is separate work.

ArbitrationCase, panel/quorum/appeal and signed split decisions remain in **#75**. Email endpoint ownership/revocation, minimal SMTP notification and large deliveries remain in **#74**. These operations do not silently bypass those missing facilities: delivery is site-only, and subjective disputed funds remain held unless the seller refunds.

## CLI and recovery

```sh
msg money balance
msg money offers
msg money redeem @exact-quote.json --request-id my-purchase-1
msg money purchase pur_example
msg bounty prove bounty_listing_id --request-id my-claim-1
msg orders create @listing-intent.json --request-id my-order-1
msg orders pay @payment-intent.json --request-id my-payment-1
msg orders history ord_example
msg orders resolve ord_example --request-id my-resolution-1
msg delivery claim @delivery-digest.json --request-id my-receipt-1
```

These commands use the normal signed client and Registry schemas, not a second API. JSON arguments accept literal JSON, `@file` or `-` for stdin. Prices are never silently refreshed. The CLI uses `money.redeem@2`; published `@1` retains its exact input schema and immediate semantics, and its short codes are never repurposed. `bounty prove` signs a validated canonical challenge, then submits its proof; retrying the same request ID reuses the same challenge request and claim ID, never a fresh nonce hidden behind an old claim ID. For uncertain results retain both the exact arguments and request ID.

Operation name alone is insufficient authority: private money/market operations also check the credential's scope against the represented account, including cached replays. New operations do not automatically extend old signed CA grants or finite credential ceilings; explicit authority renewal may be needed. Doctor reports authority snapshot gaps instead of upgrading signatures silently.

`doctor`'s read-only `market_clearing` verifies conservation, nonnegative balances, signed ledger receipts, escrow/source consistency, order history, purchase entitlements and bounty nonce/Claim linkage. Default zero supply/no banks/no offers is separately visible. `selftest`'s `market_e2e` is restricted to a fresh isolated instance and Test Root: parse and execute mint 20 and **bank fund 20**, escrow a 10 MSG bounty, pay a current-key PoP Claim, publish the fixed 5 MSG bundle, then automatically deliver under `orders.buy@4`. Before acceptance it verifies buyer 5, bank 10 and order escrow 5 MSG. The buyer reads and verifies the fixed payload, then signs `delivery.accept@2` to settle. Historical automatic settlement remains covered by `market_lifecycle` and versioned checkout tests. Its final balances are **buyer 5, bank 15, every escrow 0, supply 20 MSG**. The manifest is `msg.lmm.best store selftest`; `hello.txt` is exactly `delivery-ok\n`. It sends no external mail and does not claim SMTP acceptance testing.

Backup/restore tests include pending purchases, active entitlement grants, funded/unpaid/settled orders, delivery references, nonce consumption and Claim receipts. Restore intentionally pauses writes; bounty replay tests verify that deleting the local drill marker does not bypass containment. Old ledger-account migration remains idempotent; history is not reconstructed from mutable catalog values. Production migration, real console/SMTP/ingress and deployment acceptance remain separate, authorized work (#84).

Regression entry points: `tests/test_market_71.py`, `test_market_redemption.py`, `test_market_72.py`, `test_market_73.py`, `test_market_contracts.py`, plus existing money/admin/offers/bounty/orders/delivery, migration, hosting, dictionary, backup and conformance suites. Test totals and exact commit/CI identity belong in the PR verification record, not an unversioned completion percentage.

## Public account transparency

`money.public_balance@1` and `money.public_ledger@1` accept a stable `subject_id`.
Anonymous readers can inspect Root and currently active bank agents. Other
registered/custodial users start private and may choose publication using their
own signed `money.visibility_set@1` request. A bank's private preference is saved
but cannot hide its account while its bank role is active. Revocation immediately
reverts to that user's saved preference; archived accounts are not published.
Publication covers the full account history, including transactions before the
opt-in or bank grant. Returning an ordinary account to private hides that full
projection on subsequent reads; it cannot retract copies readers already saved.
Existing finite credential ceilings need explicit renewal before using the new
visibility operation; the server does not expand an old credential automatically.

```sh
msg money public-balance u_root
msg money public-ledger '{"subject_id":"u_root","limit":50}'
msg money visibility '{"visibility":"public"}'
msg money visibility '{"visibility":"private"}'
```

Read-only GET/HEAD views are `/@root/public-balance`, `/@root/public-ledger`, and
corresponding `/@handle/` paths. Ledger pagination uses `cursor` (last committed
sequence) and `limit` (1–100); it is ascending and uses integer minor units with
scale 6. Public items contain only transaction ID, sequence, kind, amount,
from/to account IDs and commit time. Counterparty IDs on a published account's
transactions are public even when that counterparty's own account is private;
this does not publish that account's balance or unrelated transactions. Internal
escrow account IDs may appear as counterparties, without order or delivery data.
No free-form reference, request ID, actor, signed receipt or private business
metadata is included. Public projections are informational and are not signed
receipts. The legacy `money.balance@1`, `money.ledger@1`, `/balance` and `/ledger`
remain private owner views, preserving their published schemas and receipt data.
