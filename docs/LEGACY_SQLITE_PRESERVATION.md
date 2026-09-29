# Legacy SQLite 0.22.1 offline preservation

The existing archczy service uses SQLite, unlike the earlier rewrite's PostgreSQL
market migration. This tool provides a bounded offline preflight and a transactional
**inert preservation import**. Its output is not a v4 restore archive and must never
be passed to the daemon. It does not complete the deployment migration.

## Source contract

`src/msg/data/legacy/sqlite-0221.json` records all 33 observed table layouts, including
`sqlite_sequence`, historical additive columns and column order. The synthetic SQL
fixture reproduces the read-only schema inventory of 2026-09-29, without user rows.
Installed `store.py` matches historical revision
`e12eea32764656eec82452fee111e5b4fb6d8a42` with SHA-256
`5fa50265c1b0c9674d7464aadd1cbaaf7d76d41c2f50adededa5fb9bf13871b4`.
The contract also pins source files at `ccf451c6a3097be5849d666422f70b04b81f1710`
for reference. A matching individual source file does not identify a whole deployed
package. Synthetic acceptance is not protected live-snapshot acceptance.

Historical `crypto.request_payload` binds action-specific fields; certificate
signatures use `certificate_payload(body)`. Stored post columns are not necessarily
the complete original signed HTTP request. Preservation retains every original
signature field and certificate body verbatim; it neither reconstructs missing
signed envelopes nor labels legacy signatures as valid v4 signatures.

## Offline use

First obtain a separately protected, consistent SQLite backup with its manifest,
source lineage and independent current revocation checkpoint. Do not copy only the
main database while a live WAL exists. This tool never creates that backup and
refuses source WAL/SHM/journal companions. Operate on an isolated copy with no writer.

```sh
python -m msg.storage.legacy_sqlite /protected/offline-copy.db --sha256 MANIFEST_SHA256
python -m msg.storage.legacy_sqlite /protected/offline-copy.db --sha256 MANIFEST_SHA256 --destination /protected/inert-preservation.db
```

Preflight opens SQLite read-only with query-only mode and a 60-second query budget.
It rejects unknown table/column layouts, views/triggers, unsupported user versions,
invalid attachment lengths/digests, oversized inputs and digest changes. Limits are
256 MiB source, one million rows, 16 MiB cells and 1 GiB encoded rows. JSON output
contains counts, digests and column names, never row values. Passing a digest binds
the input; it does not authenticate who produced that digest.

The destination is created exclusively with mode 0600. Every original column value
is kept in a typed row envelope: decimal integers, hexadecimal floating values,
unchanged text, base64 blobs and explicit nulls. Old post/archive IDs, reply targets,
deleted/hidden flags, nonce records, certificate/revocation rows, opaque custody and
keystore ciphertext, and dormant external queues all remain present. Table row
hashes use length-prefixed canonical JSON envelopes in the exact source scan order.
The manifest records the source and schema contract digests. Failed import rolls
back and removes the new partial destination; an existing destination is refused.
Treat this archive as sensitive as the original database, including legacy token
hashes, ciphertext and callback data. Use protected storage with equivalent retention.

## Gates before an active v4 import

The preservation database contains only `legacy_rows` and `provenance`; it creates
no v4 identities, resources, credentials, CAS references or external jobs.

An active converter still requires an explicit Root-approved identity mapping and
revocation policy, a way to represent legacy signatures without asserting new
signature validity, stable old URL/ID/reply/archive mapping, custody compatibility
or reenrollment, and attachment CAS/reference validation. Legacy SSH keys and
certificate grants cannot automatically gain new authority. Queued WebSub/webhook
work cannot be replayed by default. Filesystem Git repositories need an independent
protected inventory and ownership/ref mapping. Installed source provenance, the
protected snapshot, isolated conversion verification and rollback rehearsal remain
mandatory deployment evidence. Retain the old database and preservation archive
until those gates and an explicit backup-retirement decision are satisfied.

## Import usable content into isolated v4 PostgreSQL

`msg.storage.legacy_resource_import` implements the next stage: old boards become
private topics, post bodies become normal posts, and attachment bytes enter the
normal Git/CAS revision store. Normal `create_resource`/`revise_resource` and the
PostgreSQL transaction writer lock apply, including CAS pin rollback compensation.
This stage requires an already provisioned, isolated v4 installation. It does not
provision Root, activate legacy identities, or run the old service.

The operator first creates an empty topic owned by an existing registered v4
operator with mode `0700`. `identity_requirements(snapshot, sha256, mapping)` reports
required/missing/unexpected author mapping counts and a digest without exposing
author IDs, post bodies or keys. The rehearsal explicitly requests mapping keys
only in private process memory.
Every legacy post author requires an explicit mapping to an existing registered v4
identity; anonymous posts use the explicit key `__anonymous__`. Mapping records
historical attribution only: all imported resources remain owned by the chosen
operator, and new revisions identify that importing operator. No legacy identity
receives credentials, membership, ownership or certificate grants through this map.

Use `approval_payload` to construct the approval. It binds the exact source digest,
destination service, target topic ID and generation, operator, full identity map,
private visibility, disabled legacy authority/jobs, and expiry within 24 hours.
The existing destination Root must sign its canonical JSON with purpose
`legacy-sqlite-content-import-v1`. `import_content` verifies that signature against
the Root public key already pinned by the installation. An envelope is JSON with
`approval` (payload) and `signature` (the normal wire-format Signature). The importer
never reads or generates a Root private key and never chooses a new Root.

```sh
python -m msg.storage.legacy_resource_import \
  --config /isolated/etc \
  --snapshot /protected/offline-copy.db \
  --signed-approval /protected/root-approved-content-import.json
```

The command loads the destination installation (including normal release-source
synchronization), then imports atomically. Use only the isolated target config;
loading a production config is not a preflight. Existing imports, nonempty target
topics, changed target generations, incomplete maps, missing board/post references,
ambiguous reply targets and Root-signature mismatches fail closed. Recovery quarantine
is respected. Each imported board is `0700`, has closed membership and only the
explicit operator as administrator; posts, attachments and provenance records are
`0600`. No prior public visibility is restored automatically.

Legacy signatures, title, timestamps, actors and deletion/hidden/archive claims are
stored as separate private JSON provenance resources. A setting named
`legacy-provenance:<new-resource-id>` locates that record and the resolved reply
target. These legacy signatures are explicitly unverified; new import revisions are
unsigned and carry an operation provenance digest. The tool does not manufacture an
original request envelope from incomplete old columns. Original bodies are preserved
without embedding historical metadata into their text. Attachment relations are
normal v4 revision relations, and old active-post tags are retained.

`legacy-import:<snapshot-sha256>` stores counts, approval digest and old-to-new URL
mapping. Archived posts have separate retained mappings so they cannot overwrite
live posts. This is a mapping artifact, **not a public HTTP redirect installation**.
The report records all source table counts so excluded identity/certificate/queue
state remains visible as an outstanding migration scope. Keep the inert preservation
archive alongside this import; it retains all remaining legacy tables and columns.

Still required before switching the old service: confirm real protected-snapshot
acceptance, review the resulting content and archive/reply presentation, install and
test authorized URL redirects, decide visibility/ownership publication, migrate or
reenroll identities/custody, enforce independent current revocations, import protected
Git repository inventories, and rehearse deployment rollback. Source-synthetic tests
of this importer do not establish those host acceptance results.

### Test-only rehearsal with a real protected snapshot

`python -m msg.storage.legacy_rehearsal --snapshot /protected/offline.db
--sha256 MANIFEST_SHA256 --protected-target /protected/new-run` creates a disposable
local PostgreSQL cluster listening only on a private Unix socket. Its fresh Root,
registered importer, and many-to-one author mapping are explicitly **test-only**.
They cannot approve production imports against another pinned Root/service. This
exercises real source compatibility without deciding production identity continuity.
The target parent must already be private (`0700`), the input private (`0600`), and
the target must be new and outside temporary directories. A short target path is
needed for the PostgreSQL socket. All resulting database/CAS/config data stays under
the protected target; the stopped cluster is retained for controlled examination.

Only counts, source digest, invariant checks and stable error codes are emitted.
`summary.json` is mode `0600`; exceptions containing database row content are not
printed. The rehearsal compares source digests/table summaries before and after and
asserts credential/certificate/job counts do not change during content import.
It stops the cluster before reporting completion. A failed run is also stopped and
retained privately, with `result: failed`; it is never production acceptance.

Legacy board names reserved by v4 views are assigned deterministic safe names;
original board URL mappings are retained. Old post URL resolution accepts both the
global ID and board sequence, choosing the lowest global ID on a collision exactly
as the installed historical `find_in_board` implementation did. `/raw`, `/meta`,
and active `/file/ID` aliases are recorded too; their eventual HTTP presentation
still needs an explicitly authorized adapter, rather than a silent publication.

### Explicit HTTP compatibility namespace

Imported aliases are available at `/_legacy/<snapshot-sha256>/<old-path>`; for
example `/_legacy/<sha256>/main/17/raw`. This does not replace current `/main/17`
routing. Only a recorded private content import supplies this namespace. A target
must still belong to its approved migration parent. Moving it outside that subtree
makes the alias unavailable.

GET and HEAD reuse normal discovery authorization and return a `308` only after
checking the currently authorized resource ID against the mapped ID. Signed
`X-Msg-Request` packets identify the stable **new resource ID** and matching read
operation/view; the transport never rewrites a signed request. After authorization,
the mapping and namespace are checked again before selecting the current canonical
path. Redirects carry `Cache-Control: no-store`, the source digest, and an explicit
`unverified-historical-claim` signature label. Denied requests disclose no Location,
Link or ETag. Writes through this namespace are rejected.

A successful post redirect links to the same explicit alias with `/provenance`.
That endpoint independently authorizes its private provenance resource and redirects
to its JSON representation; permission to read a post does not grant permission to
read its provenance. The destination's actual stored signature fields are preserved
there without being presented as a valid new-format signature. Old `/raw`, `/meta`
and `/file/ID` views are mapped to the corresponding native read representations.
Production hostname switching, publishing old unprefixed URLs, identity migration
and broader visibility remain separate authorized deployment work.
