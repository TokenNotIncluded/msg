# Architecture reuse

The authoritative design approves fewer independent mechanisms, not fewer
visible capabilities. Keep published operation versions, short codes, signed
bytes, IDs and historical receipts compatible. Existing completed-contract
regressions remain binding.

## Implemented slice: order notifications

`market/delivery_targets.py` owns shared account-email lookup, address validation,
mail enablement and authenticated pickup-link construction. Legacy checkout and
v3 keep their endpoint encoding and message formats; sharing a helper does not
reinterpret their signed contracts. Endpoint generation, owner, current key and
notification opt-in are still revalidated before sending. Control characters,
URL credentials, query/fragment secrets and malformed origins fail closed.

A Delivery says what was provided to which buyer. A notification EffectJob says
whether SMTP work was queued, accepted, failed or uncertain. Neither proves buyer
acceptance or permits settlement. V3 order reads derive notification state from
the current endpoint's job and result, without editing order rows or trusting a
stale cached success. Missing jobs cannot prove SMTP delivery. Failed jobs stay
failed; disabled endpoints and uncertain attempts are not reactivated by reads.

The shared effect completion/retry/lease fence writes job facts, not market
presentation fields. Its attempt, state, deadline and current-authority checks
remain unchanged. The notification read path checks the linked job's order,
endpoint, kind and principal before displaying it. Seller views still exclude
buyer email addresses.

## Model convergence

The staged [offer resource migration](OFFER_RESOURCE_MIGRATION.md) maps new
ServerOffers to Resources and new `money.redeem@3` purchases to Orders; published
legacy versions retain their compatibility paths. The [compatibility guide](OFFER_ORDER_COMPATIBILITY.md)
describes read projections and unmapped legacy rows. Keep typed account purposes,
server entitlement authority, atomic accounting and explicit migrations; shared
helpers do not prove that every legacy row or production instance has migrated.

ConsignmentPackage is immutable content; Delivery is a recipient-bound fact.
Recovery and delivery envelopes can share codecs, not authority or secret-release
policies. Notes and similar content share Resource/Revision, while membership,
certificates, grants and money keep their controlled operations and constraints.

OperationSpec is the source of operation metadata; routes/help derive from it.
Specific business functions use the existing executor transaction. They do not
need another Engine/Manager/Repository hierarchy. Worker scheduling is shared;
channel executors and secret/network permissions remain isolated.

New default explicit acceptance requires a new operation or policy version.
Never change orders.buy@1/@2/@3 in place. Keep automatic-delivery/settlement
regressions and distinguish their policy settlement from signed buyer claims.
Money held before fulfillment may be refunded from escrow; already-settled root
funds still require the local root command. Same request and payload replay the
original non-secret result, not another payout or another one-time secret.

Refs #85, #71, #72 and #81. No production deployment or financial operation is
part of this refactor.
