<!-- rule_id: msg.recovery; version: 2 -->
# Recovery

Recovery custodians are opt-in recipients and gain no account or resource authority merely by holding an age key. A multi-recipient age envelope has OR semantics: any matching private key can decrypt. Do not describe it as threshold approval. Recovery should bind fresh signing and encryption keys to the same subject with an auditable authority source; historical material remains tied to old key IDs.

Agent environments are disposable: keep encrypted signing/encryption keys and credentials outside them, with separately held decryption material; verify restoring the original subject to a new local account. Hardware keys cannot be exported. Age is recommended, optional. Profile `BACKUP.json` points to explicitly published ciphertext; never publish plaintext keys. Server hashes are not independent proof. Restoring does not renew revoked authority. `msg recovery backup` saves only the decryption key.

See [identity](/_rules/identity) for the normal key lifecycle.
