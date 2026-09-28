# Recovery safety: issues #68–70

This patch is an isolated implementation increment, not production promotion or
acceptance of the three umbrella issues. No production data, root key, PIN,
certificate, balance or outbound endpoint was used.

## Implemented

### #68 — independently verifiable migration evidence

`identity.custodial_upgrade_inventory` now re-verifies each persisted ACK against
its original subject, challenge, mapping, plaintext digest, request ID and new
signing key. Counting rows is not verification. Changed, missing, malformed or
cross-challenge evidence remains pending. New mappings explicitly identify both
encryption subkeys; retained legacy mappings are bound through their challenge.
An inventory delta also sets `historical_revisions_require_migration`, even if
all ACKs for the old list were valid.

The original token may inspect a pending challenge; after an identity switch,
only the challenge's new signing key may inspect its completed result. The
read reports `identity_switched`, `history_recoverable`, `online_retired`,
`backup_retired`, `server_key_retired` and a separate `completion_status`.
Online retirement requires the new primary keys, retired old keys, revoked old
signing credential and empty destroyed vault fields. `backup_retired` becomes true
only for one exact root-signed record imported at the physical console and
re-verified on each read (see `CUSTODIAL_HISTORY.md`); `server_key_retired`
additionally needs online retirement. Without that record both remain false.
The legacy `status=completed` from an empty-inventory identity switch is not a
claim that every server-held backup has been destroyed; use the independent
retirement fields.

Coverage is explicitly `enumerated_keystore_age_revisions`; external ciphertext
coverage is unknown. A valid ACK is a client's signed declaration, not proof
that every unknown ciphertext or recipient is recoverable.

### #69 — transactional, persistent restore quarantine

The v4 dump is converted to a protected SQL file, then imported with the mandatory
`recovery_quarantine` setting in one `psql --single-transaction` commit. A failure
before that commit rolls back both. The SQL file is not retained. Existing v4
archives remain supported; no root private material is added to data backups.
`psql` is now required alongside the existing PostgreSQL backup tools.

Deleting the config marker, copying the configuration or restarting does not
clear this database gate. Unknown, malformed and even null/false gate values
remain quarantined. Startup does not synchronize release-owned resources while
quarantined. Authentication/authorization, SSH principals, share-link authority,
external job claims and scheduled maintenance fail closed. Process-local gating
also covers old marker-only recovery drills.

Only the HTTP health response remains available; it returns 503 with
`status=recovery_quarantined`, `ready=false`, and disabled writes/outbound.
Business reads are blocked too: a resource that was public in an old snapshot
may have become private since. Restore returns `revocation_replay=required` and
`promotion=blocked`. No network operation clears this state.

This is containment, **not** an implementation or proof of monotonic revocation
replay. Do not remove the gate to promote an unverified restore.

### #70 — root rotation storage and restart integrity

Rotation now writes the new encrypted root key to the same protected directory
used by `root_envelope` and the pending journal, rather than creating an unrelated
`config/root/key.json`. New independent layouts remain independent; existing
legacy layouts retain their explicit lookup behavior.

A v2 journal signs commitments to the old certificate, new trust document,
encrypted envelope, exact online CSR and prior envelope digest. Resume verifies
those commitments, self-signature, CSR possession proof, service/subject/key
bindings and the already committed database state. Old key history remains
0600 under 0700 directories, is not overwritten on retry, and is never replaced
by an already installed new key. The audit stores public commitments, not the
encrypted root envelope. A pending v1 journal is rejected with
`rotation_journal_upgrade_required`; it is not silently treated as a v2 approval.
Such a journal needs a separately reviewed local recovery based on its actual
commit/file stage, not merely a version-number edit.

Root operations remain restricted to the existing local-console boundary.
Old chains remain invalidated on rotation; no automatic dual-trust window or
silent expansion of existing signed grants is introduced.

## Evidence and outstanding acceptance

Local evidence: 34 cryptographic/state/SQL-command unit cases passed on Python
3.13.5; compilation and `git diff --check` passed. This does not substitute for
the project's Python 3.15/PostgreSQL/age CI. Added integration regressions cover
real age decryption before ACK, completed identity versus pending backup
retirement, deleted-marker restore/restart, no outbound jobs or maintenance,
atomic restore failure, and root rotation interruption before key/trust writes.
CI results must be taken from the actual run, not this document.

#68 remains open for complete migration finalization, frozen-scope deltas/new-write
key enforcement, compatible old-encryption-key envelopes, irrecoverable-item
choices, and independently auditable backup retirement. #69 remains open for a
durable independently trusted anti-rollback checkpoint, monotonic replay of all
revocation/retirement/authorization facts, controlled promotion and a production
recovery drill. #70 remains open for real OS-console acceptance, operator-approved
existing-CA governance/migration, and a reviewed trust-transition/rollback plan.
