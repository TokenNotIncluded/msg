# Market boundary progress

## Scope and coordination

Issue #159; references #85/#83, not completion of #71/#72 or production acceptance.
Base: `476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7`, tree
`40ca207a3c49be347120e058e47ed328ec4e3098`. Branch:
`fix/market-boundaries-20260929`. Coordination is #83 comment5878359290.

This work does not modify #165/#167 core/tool/executor ownership, #155 recovery,
or #166 metadata session extraction. Main and all active claims were refreshed
before starting. No production, real funds, Root/PIN or outward delivery action.

## Regression first

The first commit changes no production implementation. It adds ten tests:
seven ownership/import/unique-guard checks and three real PostgreSQL behavior
contracts. Current expected failures concern the new module boundaries; expected
passing characterizations cover buyer/seller privacy, forged escrow authority
with no ledger changes, receipt verification/byte preservation and replay.
No test result is claimed until the exact cloud run is inspected.

## Observed dependency graph

`market.orders/email/arbitration/delivery -> plugins.orders` for order records,
subject checks and existing projections. `market -> plugins.store` for listing
and package reads. `market.escrow/orders -> plugins.money` for protected posting.
`market.policy/targets/notifications/escrow/delivery -> plugins.delivery` for
managed delivery records and verification. `plugins.orders/delivery -> market.escrow`
closes the reverse dependency. Function-local imports are included in this audit.

## Intended ownership and invariants

- `market.order_records`: signed subject/viewer checks, order IDs, authorized
  record lookup and explicitly legacy-compatible projections.
- `market.catalog`: existing authorized listing/body/package reads, not plugin
  registration or version-specific catalog schema/defaults.
- `market.ledger`: existing policy constants, account requirements and one posting
  implementation. The in-process escrow guard has one owner; moving it does not
  make it a credential or a network operation. Local-only Root issuance is unchanged.
- `market.managed_delivery`: existing bounded managed-package verification,
  local preparation and read helpers. Their published version semantics remain;
  they do not replace v3 service/manual delivery or arbitration state machines.

Old plugin imports re-export these same functions, never wrappers with new policy
or copied implementations. Version-specific schema, request signing and response
adaptation stay at their existing entrypoints. Existing transactions, account
locks, receipt purposes, ID generation and recovery/lease fences remain intact.
No new Manager, Factory, Repository, ORM or alternate money/escrow path is added.

## Verification and integration

Focused cloud Actions run the new contracts and existing checkout v1/v2/v3,
account-scope, deterministic clearing, Bounty, redemption, storage and recovery
suites. Full four-shard exact node-ID/conformance/build and applicable recovery
checks remain required on the final synchronized head. No skips, weakened
assertions, changed published versions or historical fact/schema migration.
