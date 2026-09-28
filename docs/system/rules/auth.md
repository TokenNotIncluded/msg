<!-- rule_id: msg.auth; version: 2 -->
# Authorization

Authenticate the request, then check credential ceiling, current certificates, resource ancestry, and the specific operation. Ownership, group membership, or a historical signature does not bypass a credential ceiling. Special capabilities and CA delegation only apply within their signed scope and live authority chain. A Topic admin has Topic governance rights, not system, organization, or CA authority.

Private money, order and delivery operations require the current credential to cover its own subject account, including cached-result retries. A store-scoped key is not account authority. Buyer, seller and panel checks still apply. Public dispute summaries remain minimal and public.

See [identity](/_rules/identity) for key roles and [security](/_rules/security) for local-only root boundaries.
