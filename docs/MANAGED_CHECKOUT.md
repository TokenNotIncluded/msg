# Managed checkout contracts

This describes the bounded implementation for issues #74–76, not completion of the entire market or authorization design. No deployment or authority-policy change is implied.

## Versioned purchase and acknowledgement

`orders.buy@1` retains its existing signed quote and funded-escrow behavior under `escrow-v1`. Existing orders and signed policy digests are not silently reinterpreted.

`orders.buy@2` requires the same listing ID/revision, currency, quantity and total price, plus the immutable `package_digest`. It supports active `managed_instant` file/bundle packages up to 1 MiB. The executor funds the order and prepares the verified in-site package in the same PostgreSQL write transaction. It does not call SMTP during checkout.

Optional `auto_accept: true` is accepted only for a listing explicitly using `escrow-instant-v1`. The buyer's real signed checkout must bind the exact package digest and opt-in. The objective EscrowEngine signs and records the release decision, source request proof and ledger reference in the same transaction. A domain-separated internal ledger request ID distinguishes the release leg from the funding leg while the decision retains the original signed request ID. Request replay returns the original result; a distinct last-unit purchase cannot oversell.

Without this opt-in, `orders.buy@2` prepares the package but keeps funds in escrow. The existing signed `delivery.accept@1` releases escrow and acknowledges the verified delivery. With instant settlement, the delivery remains `prepared`; a later signed `delivery.claim@1` binds `order_id` and `delivery_digest`, verifies the committed release and marks it claimed without another transfer. The old decision execution deadline does not prevent later acknowledgement of a previously committed payment.

`delivery.get@1` is a read. Neither HTTP success, a prepared package nor SMTP acceptance is evidence of a signed buyer acknowledgement. The authoritative site target is exactly `{subject_id: buyer, channel: site}`. Preparation, reading, settlement, claiming and mail projection recheck it. An order ID or link is not authentication.

## Optional email, separate from delivery

The optional signed `email` checkout field is per-order opt-in, not permission to overwrite the buyer's identity settings. If SMTP is disabled, the site transaction still completes. An address not currently verified for this buyer remains pending and sends no order or product information. To activate it, the buyer explicitly uses the existing `identity.email_set` / `identity.email_verify` challenge flow, then signs `delivery.notify@1` for the order. Checkout itself never sends a verification challenge to a new address.

The private notification fact binds the immutable original address, endpoint owner, endpoint generation, verified-at value and checkout username snapshot. A worker rechecks current credentials/authority, order buyer, delivery target, verified package, notification opt-in, endpoint owner/address/generation and mail configuration before projecting a message. Disabling a pending order notification uses `delivery.notify` with `enabled: false`. An endpoint change or re-verification does not silently retarget an already queued message.

The fixed subject is `msg order ready`. The body has only three lines: order number, checkout username snapshot and `/_orders/{order_id}/_delivery` under the configured service origin. The link requires fresh authorization. There are no attachments, goods, access tokens, private content references, seller titles or caller-selected message fields. Sellers cannot read the buyer's email through order projections.

Observable notification states are `disabled`, `pending`, `queued`, `smtp_accepted`, `failed` and `delivered_unknown`. A definite pre-send connection failure uses bounded backoff. Ambiguous external delivery or an expired execution lease is not blindly retried. Mail outcomes never debit/refund escrow, reverse site delivery or mark it claimed. Repeated `delivery.notify` requests do not create a second job for an attempted order notification.

## Verification and remaining work

The regression tests use the real signed executor and PostgreSQL. Only the external SMTP sender is replaced by a sink. Coverage includes immutable package/target mismatches, post-prepare target corruption, concurrent/replayed checkout, late decision/outbox failure rolling back all transactional facts, delayed signed acknowledgement, endpoint/credential/preference revocation, minimal mail fields and the prefunded 10 MSG bounty followed by a 5 MSG bundle purchase.

`tests/test_share_transport_matrix.py` runs the same group-reshare revocation scenario through real HTTP, GET path, GraphQL, MCP HTTP, CLI subprocess and MCP stdio adapters. It exercises current content, historical revisions/history, warm known-digest reads, lexical search, Sync revocation and independent owner grants. It does not mock transports or relax a refusal to make an adapter pass.

This is not the complete authorization-source product matrix: organization/Topic roles, certificate combinations, ShareLink, concurrent restoration and every attachment/notification projection still need their broader acceptance cases. ShareGrant@2 continues to support read-only grants and empty constraints; arbitrary constraints are rejected.

Full dispute cases, evidence roles, conflict/panel/quorum selection, arbitrator-signed split/refund/release, appeals, deadlines and the complete recovery/doctor/selftest matrix remain outside the objective EscrowEngine. Larger packages, service delivery, encrypted-secret goods and a dedicated order-inbox reference projection are not provided by this slice. Keep umbrella issues open until their own acceptance criteria are met.

New installations derive supported operations from the registry. Existing CA policy, certificates and credential ceilings do not automatically gain `orders.buy@2`, `delivery.claim@1` or `delivery.notify@1`; operators must explicitly review and sign any authority update before enabling the flow. Review stored historical policy labels before deployment; unknown escrow policies fail closed. Production credentials, money and mail configuration are unchanged by this code change.
