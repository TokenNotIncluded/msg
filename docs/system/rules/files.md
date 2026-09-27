<!-- rule_id: msg.files; version: 1 -->
# Files and secrets

Transfer chunks and sealed content follow their own size, digest, and authorization checks. A keystore item holds client-encrypted third-party data or an explicit RecoveryEnvelope; it is not a place to upload plaintext private keys or tokens. The existing msg X25519 envelope is not age. Reading ciphertext or metadata requires current resource access.

See [recovery](/_rules/recovery) before adding custodians or recovery recipients.
