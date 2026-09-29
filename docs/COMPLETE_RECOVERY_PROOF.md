# Complete recovery state proof

`msgd root recovery-proof` is a physical-console-only recovery ceremony. Signing
and promotion both require the existing local OS administrator/console checks,
confirmation of the displayed exact digest, and local Root PIN entry. The PIN
is never written to a proof, receipt, log, or command argument.

## What this path proves

The current authoritative instance signs a separate, versioned
`complete-recovery-state-v1` statement. It commits to all 86 supported metadata
tables, exact columns and schema definitions, typed row counts/digests (including
duplicates), sequence positions, storage references, actual content/Git/LFS trees,
trust document, normalized configuration policy, and random service-key digests.
Configuration normalization excludes only listen/port, deployment paths, database
connection, Valkey and mail settings. The latter two must remain disabled on the
recovery target. Plugin, limits, tool sandbox/network policy, TTLs and all other
policy fields must match; a hosting marker is bound by presence rather than its
deployment-specific path. Configuration is reloaded to reject cached-policy drift. Credentials, certificates,
revocations, policies, scope/ownership, task/nonce/ledger/settlement data,
authorization epoch and the full audit chain are included. Usable certificate
chains, current Root/online/receipt keys, resource ancestry and ledger/market
invariants are checked again before approval.

The fixed packaged schema vocabulary includes the current market migration
(`server_offer_resources` and bigint order quantities). PostgreSQL 18 duplicates
NOT NULL column flags as named constraints; vocabulary comparison normalizes
those redundant entries for PostgreSQL 16 compatibility, while exact column
nullability and every signed catalogue definition remain bound.

The fixed packaged schema vocabulary rejects unknown schemas, relations, columns,
indexes, triggers and functions. Row-level security, policies, rewrite rules and
custom public collations are rejected; column/database collations are bound. Full definitions must also match the signed
source. There is no caller-selected table list or `complete=true` bypass.

Exactly three local control settings are omitted from row digests:
`recovery_quarantine`, `recovery_replay`, and `recovery_promotion`.
No prefix is omitted. For an initial promotion, only the two restore pause fields
`runtime_config.accept_writes` and `runtime_config.cleanup_enabled` are normalized
to the signed source values, after proving both actual restored fields are false.
All other runtime settings remain exact.

This is a strict matching path: the restored/reconciled state must equal the
independently committed complete current state. It does **not** rebuild arbitrary
missing grants from a partial replay log. Earlier partial authority replay
receipts remain incomplete; their receipt alone cannot authorize promotion. A
state with extra recovery audit entries will not match an earlier complete proof.

## Operator input

1. Create the backup on the current authoritative instance.
2. On that instance's physical console:

   ```sh
   msgd root recovery-proof sign BACKUP_SHA256 SEQUENCE proof.json
   ```

   The command drafts the commitment, displays its digest, asks for confirmation,
   then rechecks the entire state after Root unlock before signing. Any concurrent
   change requires a new draft. Statements expire after at most 24 hours.
3. Obtain an **independent current pin** outside the restored backup and outside
   the proof packet. Its protected JSON file contains exactly `service`,
   `public_key` (base64 Root public key), `digest`, `sequence`, and
   `source_backup_sha256`. The operator must establish this pin's freshness and
   provenance through their existing trusted channel. Copying fields from the
   untrusted packet is not independent verification. Software cannot manufacture
   evidence that an external source is current.
4. Restore into the isolated target. On its physical console, with the legitimate
   Root envelope locally available:

   ```sh
   msgd root recovery-proof promote proof.json independent-pin.json
   ```

   Inputs must be regular owner-only protected files. The target must have both
   its database quarantine and recovery marker. Exact state/pin/signature/key
   checks must succeed, then the local operator approves the digest and enters
   the Root PIN. No network command or automatic worker performs this ceremony.

## Failure and restart behavior

Metadata checking and the initial promotion delta occur under the application
writer lock and exclusive locks on every supported table. The Root-signed
promotion receipt binds the exact new audit row, generation and prior audit
sequence. Audit insertion uses an explicit transactional sequence number;
PostgreSQL's nontransactional sequence is advanced only after the receipt commits,
while the filesystem marker still blocks requests. Retries accept only the exact
prior/final sequence states, rehash all metadata/files, and exclude only that
signed audit tail and generation delta. Extra rows or altered pending receipt/state fail closed. The signed generation
row must actually exist; absence never means a successful restart fence. A backup
of a previously promoted source may contain a historical receipt: only a receipt
for the current independent manifest can resume; a new manifest must instead
match the complete initial state before creating its own receipt.

The database gate remains active until marker removal and directory fsync succeed
under the final transaction lock. Marker removal failure leaves both barriers
closed. A crash or fsync failure after unlink leaves the database gate closed;
a signed exact receipt permits retrying with the marker missing. A receipt alone
does not skip revalidation. Promotion atomically writes a fresh
`recovery_runtime_generation`. Every previously loaded application/hosting runtime
must remain stale and be recreated; restarting only one cached worker is not
sufficient. A fresh runtime is required after successful promotion.

As with backup/restore, the local OS administrator controls the storage directories
and PostgreSQL administrator boundary. Keep the target isolated from independent
filesystem writers and direct SQL administrators during the ceremony. This proof
does not attest to an external backup having been physically erased, nor to an
operator's independent pin having been obtained honestly. Real retirement
attestations, hardware Root access, and production promotion remain separate
operator evidence, not claims made by synthetic test fixtures.
