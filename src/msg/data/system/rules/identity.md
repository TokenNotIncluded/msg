<!-- rule_id: msg.identity; version: 2 -->
# Identity

`subject_id` is stable; `@handle` can change. Distinguish actor, subject, and author. A self-custody client holds its Ed25519 IdentityKey and independent age/X25519 EncryptionSubkey privately; the server stores public verification material and the age recipient. Do not derive one key from the other or from SSH keys. Historical signatures and encrypted data stay bound to their original key IDs. Registration and key changes use the declared signed Operation, never a normal file write.

Account follows are directed and public for readable profiles; mutual follows grant no extra permission. Private resource watches are separate. `/feed` uses public posts, explicit follows and request interests; no click tracking or inferred profile.

See [authorization](/_rules/auth) and [recovery](/_rules/recovery).
