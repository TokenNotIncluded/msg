# Custodial history recovery and key retirement

A successful identity switch is not proof that all history is readable or that
all server-held copies of a key are gone. The migration inventory reports these
facts independently. `status=completed` keeps its historical API meaning of
**identity switch**, with `completion_scope=identity_switch_only` explicitly
included. It is not an assertion of complete key retirement.

## Owner workflow

The client saves a protected local journal and proves possession of both the new
identity key and the new age key. The server then changes the primary encryption
target. Writes during migration must identify that new key; old-key or unlabelled
age writes fail closed. Another migration or encryption rotation cannot replace
the bound target. The current token stays active until the explicit final decision.

```sh
msg identity upgrade-custodial new-handle --external-history unknown
msg identity migration status
msg identity migration migrate --method rewrap --limit 100
msg identity migration status
# Only after checking history, policy and external copies:
msg identity migration finalize --resolution verified --external-history migrated
```

`migrate` works in bounded batches and never finalizes automatically. It decrypts
returned ciphertext **locally**, then signs an ACK over the exact source and target
Refs/Revisions, key IDs, ciphertext and plaintext digests, method, inventory digest,
challenge, subject and request ID. Plaintext is not sent back. A legacy unscoped ACK
remains readable but is insufficient to finalize a scoped inventory.

`status` lists every retained owned age keystore revision, including non-current
history. Outputs are separate private resources; neither source revision bytes nor
its current pointer/signature are overwritten. A new source, missing source or
changed result invalidates a finalization preview. `refresh` signs the observed
delta, preserves missing items as unresolved, and increments the inventory version.
Every retained item must be ACKed again for that new version. A caller-declared key
ID on a new ciphertext is not a decryption proof.

## Two recovery routes

**A — rewrap.** Each mapping points from one immutable source revision to one new
private ciphertext encrypted to the proven new recipient. The official client
actually decrypts the target before signing its scoped ACK.

**B — compatibility recovery.** `msg identity migration envelope` returns an
owner-private age-encrypted envelope containing only that owner's retired
EncryptionSubkey. It never exports an IdentityKey, vault master secret or another
owner's key. `migrate --method compatibility_recovery` decrypts the envelope locally,
validates its owner, challenge, key ID and recipient, then decrypts every selected
old ciphertext before ACKing it. The old key exists only in a protected temporary
client file during this verification. This route is labelled
`compatibility_recovery`, never re-encryption. Protect the new age identity and
recovery envelope: together they intentionally recover historical ciphertext.

## Unresolved history and explicit decisions

Unidentified, missing or undecryptable revisions stay unresolved. A final decision
is signed by the proven new identity key and binds the current inventory, actual
observed inventory, result digest, policy version and complete loss list.

`--resolution accept_loss --loss-revision REV --reason TEXT` requires exactly the
unresolved revision list and a nonempty reason. It does not label that history
recoverable. An external-history declaration is an owner statement, not verified
coverage of files outside the enumerated server history.

`--resolution retain_decrypt --loss-revision REV --reason TEXT --external-history unknown`
can switch identity while keeping **only** the retired encryption key for later
recovery. All old tokens/signing credentials are revoked and the encrypted signing
key material is deleted in the same transaction. PostgreSQL prevents a
`decrypt_only` vault row from retaining signing material. Only the new identity key
can continue that migration; after verification it can explicitly finalize again
to remove the remaining online age key. An unknown old database constraint is not
silently dropped by the schema migration.

## Crash boundaries and evidence

The client writes/fsyncs an exact signed decision intention before sending. A lost
final response is recovered using the **new** identity key and the original
finalization request ID; the old token is not resurrected. Accepted client identity
state is saved before the pending journal is removed. The protected
`custodial-history.json` remains as the stable entry point for later inspection or
completion of decrypt-only retention.

Audit events distinguish token revocation, online signing/encryption-key deletion
and the scoped migration decision. Doctor's `custodial_history` check is read-only;
its output lists phases, unresolved counts, drift and online retirement facts.
The `custodial_history_recovery` selftest creates real age history in the disposable
Test Root, rewraps, locally decrypts, ACKs and finalizes it. It refuses a production
application without the isolated selftest namespace.

`history_recoverable` covers **enumerated and client-verified revisions only**.
`online_key_retired` describes online vault material only. `backup_retired` and
`server_key_retired` remain false until a separate independently verified offline
retirement procedure exists. Neither a boolean supplied by a network caller nor a
green test run can establish that old backups lost a key. Restore/revocation replay
and real operator acceptance are tracked separately in issues #69 and #70.

## Published contract compatibility

The original v1 short-code rows and input schemas remain immutable. Scoped decisions use `identity.custodial_upgrade_finish@2`, scoped acknowledgements use `identity.custodial_rewrap_ack@2`, and explicit new-key writes use `keystore.put@2`. Existing deployments must explicitly authorize these new operations through local CA governance and reissue constrained credentials before using them; registry vocabulary never silently expands an existing signed grant. The official migration client selects v2. Legacy ACKs remain readable but cannot authorize retirement.
