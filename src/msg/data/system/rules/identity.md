<!-- rule_id: msg.identity; version: 4 -->
# Identity

`subject_id` is stable; `@handle` can change. Clients privately hold independent Ed25519 IdentityKeys and age/X25519 EncryptionSubkeys; never derive them from each other or SSH keys. Historical signatures and ciphertext retain original key IDs. Registration and key changes require signed Operations.

`#bot` is an account-internal message label. `~suffix` is a client-keyed task identity: act for its grantor within a live delegation's scope and lifetime; no permanent upgrade. Revocation invalidates descendants; redelegation defaults off and only narrows authority. Successful-use limits commit atomically; failures and retries do not consume another use. History survives expiry.

Public follows grant no authority; private watches are separate. `/feed` uses public posts and explicit follows/interests without tracking.

See [authorization](/_rules/auth) and [recovery](/_rules/recovery).
