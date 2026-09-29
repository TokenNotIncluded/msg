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
