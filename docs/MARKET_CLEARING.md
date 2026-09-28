# Clearing, bounty and order contracts

This page records the current clearing and bounty implementation and its integration with versioned market contracts. It is not production deployment approval. Currency is `primary`, scale **6**; `MSG` is a display name, not a fiat peg. Initialization creates zero supply, no bank, no server offer and no listing.

## Local central bank

```sh
msgd money mint 20
msgd money bank fund @bank-test 20
msgd money bank remove @bank-test
```

`bank fund` resolves the stable subject, displays **two** separately digest-bound previews (grant BankRole; transfer existing Root funds), then requests the hidden PIN. Both confirmations precede unlock. The role, transfer and two audit records commit together; rejection, changed preview state or insufficient funds leaves neither a new role nor a transfer. Removing BankRole does not confiscate a balance. Mint/burn, ordinary Root transfer and offer changes retain their own confirmation.

The production command requires the existing OS-root physical-console gate. SSH, PTYs, network adapters, `--yes`, PIN arguments, environment variables and piped confirmation are not substitutes. `_confirmed_money` is an internal use-case, not a remotely registered operation. Its isolated selftest replaces only console IO and the disposable Test Root unlock; production keeps `require_local_console` before loading the use-case.

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

Activation first moves the publisher's budget to a keyless BountyEscrow. `signature_pop_v1` challenges bind listing, claimant, random nonce, issue/expiry times and verifier version. TTL is **300 seconds**. Both issuance and claim require the claimant's current primary IdentityKey. Rotating/revoking it invalidates pending proofs; a signature from a different key cannot redeem one.

A paid Claim, nonce consumption, escrow transfer, signed receipt, Event and minimal Inbox reference are one transaction. The publisher need not stay online. Failed proofs do not consume budget. Concurrent last-budget claims, per-subject limits, close/refund and repeated request IDs are serialized. Top-up cannot silently resume a publisher pause or a reached maximum claim count. `bounty.get` and `store.listing_get` share the same current-state projection; immutable listing revisions retain their historical values separately.

**PoP proves control of the current signing key, not humanity, one-person-one-account or Sybil resistance.**

## Versioned orders, delivery and disputes

See [Market contracts](MARKET_CONTRACTS.md) for the existing mail, large-delivery,
evidence and arbitration implementation. Purchase versions differ intentionally:

| Operation | Managed delivery and funds release |
| --- | --- |
| `orders.buy@1` | Buyer prepares the legacy delivery and explicitly accepts it. |
| `orders.buy@2` | Requires package digest; prepares delivery at checkout. Acceptance is explicit unless `auto_accept=true` binds `escrow-instant-v1`. |
| `orders.buy@3` | Pins a v3 contract. Managed goods are delivered and settled under its policy; sealed/manual and service orders require acceptance. |

New design-default explicit-acceptance checkout is still pending, not a renamed
v3. Published requests, policies, prices, terms and signed receipts stay unchanged.

`orders.create@1` creates an unpaid v3 quote; `orders.fund@1` binds its digest,
exact total and currency. Default funding/delivery windows are 900 seconds/24
hours. Active quotes count toward listing quantity at creation, not unlimited
stock-free reservations. `orders.cancel@2` cancels unpaid v3 quotes. Later edits
to a listing do not replace the locked terms or package.

Automatic settlement leaves delivery prepared, not acknowledged. V3 buyers sign
`delivery.accept@2` with its exact digest; settled funds are not paid twice.
Legacy `delivery.claim@1` retains managed-v2 semantics. Downloads, GET/HEAD, SMTP
and Transfer reads never acknowledge or settle. Escrows are keyless non-Subjects.

`orders.dispute_open@1` takes an order ID and reason, checks objective faults and
otherwise opens a private case. `orders.dispute_execute@1` requires a case and
signed quorum decision, not a chosen refund amount or payee. The default empty
arbitrator policy holds subjective disputes instead of inventing a decision.

## CLI and recovery

```sh
msg money redeem @exact-quote.json --request-id my-purchase-1
msg bounty prove bounty_listing_id --request-id my-claim-1
msg store package pkg_example
msg orders create @listing-intent.json --request-id my-order-1
msg orders pay @payment-intent.json --request-id my-payment-1
msg orders contract ord_example
msg orders dispute @order-and-reason.json --request-id my-dispute-1
msg orders case case_example
msg orders resolve @case-and-decision.json --request-id my-resolution-1
msg delivery get ord_example --contract-version 2
msg delivery accept @delivery-digest.json --contract-version 2 --request-id my-receipt-1
```

`pay`, `dispute` and `resolve` call `orders.fund`, `orders.dispute_open` and
`orders.dispute_execute`. `contract` reads the authorized contract projection,
not history; `store package` supplies `id`, not `package_id`. Unregistered
`orders.history`, `orders.refund` and `orders.recipient_key` are not advertised.

JSON accepts literal JSON, `@file` or `-` for stdin. `--contract-version` selects
an explicit published version without changing input. Defaults remain v1 except
`money redeem` uses v2; `--contract-version 1` selects original redemption.
Prices are never refreshed silently. Retain exact arguments/request ID after an
uncertain response. `bounty prove` reuses its original challenge and claim IDs,
never a fresh nonce behind a retried claim.

Doctor checks conservation, signed receipts, escrow/source consistency,
entitlements and bounty linkage without writes. Fresh isolated `market_e2e`
uses Test Root mint/bank-fund20, prefunded PoP reward10 and the fixed 5 MSG bundle:
`msg.lmm.best store selftest`, `hello.txt` containing `delivery-ok\n`. Existing
v3 settlement ends at buyer5/bank15/escrows0/supply20, prepared-not-claimed.
It sends no external mail and does not substitute for the pending new acceptance
version, real SMTP or physical-console evidence.

Restored snapshots stay quarantined; deleting a config marker cannot reopen old
permissions. Tests compare retained ledger/claim/purchase/entitlement/content
facts offline while external requests/workers remain blocked; live authorization
checks precede backup. Controlled promotion and independent current-policy
proof remain unfinished. Old receipts are not reconstructed or re-signed.

Regressions include `test_market_71.py`, `test_market_72.py`,
`test_market_redemption.py`, `test_market_contracts.py`, `test_market_lifecycle.py`
and `test_market_cli_bindings.py`, alongside existing storage/client suites.
Exact source/CI results belong in PR evidence, not a completion percentage.

## Account-scoped market authority

Private `orders.*` and `delivery.*` registrations reuse the existing money
`account_requirements` before the executor reads an idempotent result. A key
limited to `/store` cannot spend the owner's balance, read their orders or
acknowledge their delivery; include the subject account and any required listing
scopes explicitly. Buyer/seller/panel and current resource checks still apply.
The deliberately public `orders.dispute_summary` retains its minimal projection.
`tests/test_market_account_scopes.py` checks versioned purchases, current-scope
replay, actual HTTP/PathGET/GraphQL/MCP reads and conditional GET/HEAD aliases.
It does not change order policies, published inputs, short codes or signed history.
