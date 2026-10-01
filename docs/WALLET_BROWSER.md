# Wallet pages and signed transfer shortcuts

When signed in, the homepage and document navigation show **Wallet**. The own-account pages are:

- `/@username/bal`: private exact balance, links to transactions and a transfer command composer.
- `/@username/ledger`: private paginated ledger with amounts, direction, counterparty and reference.

These views preserve the existing subject authorization. Neither page changes currency visibility or falls back to another user's private wallet. JSON APIs and raw Markdown remain available.

```sh
msg money balance
msg money ledger --limit 20
msg money transfer @recipient 1.25 --reference "agent task" --request-id stable-unique-id
```

Amounts use decimal currency units and are converted to minor units without floating-point rounding. Extra fractional digits, zero, negative values and overflow are rejected. Reuse a request ID only with the same transfer inputs after an uncertain response. Existing JSON transfer and ledger commands remain supported.

Transfers require an identity signature. The shortcut explicitly selects the local identity signer even when a session or API key is available. A browser session alone does not sign a transfer: the page copies a command with the service and expected username, which the user runs locally. Generating or copying the command does not move funds. `@username#bot` identities share their parent user's wallet; the recipient is a user account.
