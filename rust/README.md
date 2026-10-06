# Native Rust server migration

Python keeps the CLI, administrator/migration scripts, and development helpers.
Native Rust code is not yet a replacement for the running Python service. Do not
change production entry points until the remaining executor, transport, content-recovery,
worker, and deployment gates below pass.

## Fifth implementation slice: native Git/blob content storage

`msg-storage::content::GitContentStore` ports the existing `private.git`, `index`,
`binary`, `pins` and `revisions` layout without invoking Python. It streams
admitted input through 64 KiB buffers, checks optional SHA-256 expectations before
publication, uses direct Git plumbing for text/JSON/templates, and preserves
existing binary inodes (including LFS hard links). Index replacement and published
objects/ref updates are durable before success; failed input cannot publish an
index. Binary publication is on the destination filesystem, including when
staging is configured elsewhere.

Reads verify a private snapshot before delivering any requested range. This uses
constant buffered memory but O(blob size) temporary disk and full-object I/O, even
for a small range. The future adapter must enforce admission, disk and concurrency
quotas; this is not a free random-access optimization. Pins use the existing
hashed lease/digest names. Revision trees contain the same canonical manifests,
content and parent commits. Existing Python commits are reused unchanged; newly
created native commits use the revision time for deterministic retries. A reused
revision ID with different content/parents is rejected rather than overwritten.

Git runs with a cleared environment, no inherited/global Git configuration or
hooks, bounded captured output and a kill-and-wait timeout. Git stdin/stdout use
private files rather than deadlock-prone pipes. Paths/indices reject traversal,
symlinks, non-regular files and malformed object IDs. Configured directories must
be service-owned and protected from hostile writers: this is **not an openat
sandbox against same-UID races**. Existing shared-reader layouts must already
have the required modes; no implicit permission migration is performed.

The API is synchronous, Unix-only and a **trusted storage primitive**. Callers
must supply byte quotas and retain the metadata writer fence until the call has
actually finished; abandoning a blocking task does not cancel Git. Input-reader
cancellation and request scheduling belong to the still-pending adapters.
No handler, listener, root administration or production entry point is changed.

Fourteen native tests exercise success, corrupt objects/indices, quotas, failed
input, ranges, hard links, path confinement checks, shared modes, pins, immutable
revision retries and actual Git timeout/reaping. The Python differential suite
has **170 checks per profile**, importing the actual Python store and reading and
writing the same disposable directories. It covers both directions, Git objects,
canonical manifests/parent graphs, cross-language pins, a 4 MiB streamed blob,
hermetic Git and real process kills before publication and after a lost response.
`content_check` is a marker-gated local fixture, never an operation endpoint.

These tests do **not** implement LFS shared-object publication/collection, a
cross-store metadata/content recovery journal, SIGKILL cleanup or an authorized
executor. Interrupted staging is deliberately retained, not blindly collected.
See [the fixed migration checklist](MIGRATION.md) for completed/remaining scope.

## Fourth implementation slice: resource authority and PostgreSQL

`msg-identity::authorization` implements transaction-bound resource policy:
ordinary mode/group checks, ancestors, runtime/quarantine fences, current
certificate ceilings, managed-resource rules, topic roles and bans, direct
conversation participants/blocking, tool gates, and exact resource sharing with
live source-chain and owner/credential checks. Authentication alone is never a
permission to execute. Replayed results must still be checked against current
resource access by the future executor.

`msg-storage::postgres` opens the existing Python-migrated database without
creating or upgrading any schema. The startup checks cover required columns,
exact primary/unique keys, required foreign keys, and the unique root index.
Each request owns a dedicated connection and repeatable-read snapshot. Writers
hold Python's same session advisory lock until rollback compensation finishes.
Transaction-pooled proxies are not supported. Remote connections require TLS
with certificate and hostname validation, with no plaintext fallback; unsupported
libpq connection options are rejected rather than silently ignored.

The native PostgreSQL session implements authority reads, canonical resources
and hexadecimal aliases, migration-only old paths, revisions/relations, tags,
optimistic resource updates, identities/memberships/email settings, credentials,
CSRs/certificates, transfer/chunk metadata, events/audit, results, and durable job
records. These typed storage calls are not business operations. They cannot
replace the still-pending authenticated executor, issuance/refresh workflows,
blob/Git recovery or effect runner.

All typed writes poison the transaction on failure, including validation errors
that a caller catches. Nested savepoints can recover an aborted inner unit only
after successful rollback. A lost COMMIT acknowledgement returns an uncertain
outcome, retains potentially referenced external data, and releases no secrets.
No network-facing handler uses these local test drivers.

New differential coverage consists of 878 resource-policy checks against each
backend (468 are a mode/identity/check matrix), 102 PostgreSQL record comparisons,
2 additional native safety checks, 13 real process/protocol-failure checks, and
4 malformed-schema checks per build profile. The PostgreSQL tests use actual
Python-created PostgreSQL databases, not a SQLite simulation. A local test-only
wire proxy drops an acknowledged COMMIT to verify real uncertain-response and
same-ID retry behavior. All fixture databases have generated names; the harness
refuses nonlocal servers and never truncates an existing application database.

The CI workflow repeats the old and new suites in debug and optimized builds,
records the tested source SHA/tree and dependency lock, and uploads the reports.
Independent cloud results must be read before claiming CI acceptance. The
one-time offline editor export workflows are removed.

## Ownership boundary

Retain Python `msg` CLI, administration/migration scripts and developer tools.
Migrate server-side `msgd serve`, `msgd worker`, domain/identity, storage,
HTTP/MCP and crypto/protocol to Rust. Do not rename endpoints or invent a second
signature protocol. The tests import the actual Python implementation in the
same checkout, rather than a translated copy of the expected behavior.

The original inspected main baseline is
`7e16ca75ff6af4313365814c23912b5997b222c5`. This slice extends the phase 3 implementation at
`fffea47606cf` on the existing draft PR #235.

## Implemented natively

| Crate | Implemented responsibility | Python reference |
|---|---|---|
| `msg-core` | Bounded strict JSON, duplicate rejection, canonical numbers/Unicode, lossless typed JSON, SHA-256/base64url | `core/codec.py` |
| `msg-crypto` | Existing key/subject IDs, purpose-framed Ed25519 signing/verification | `security/crypto.py` |
| `msg-protocol` | Request shape/defaults, UTC time, business digest/signature bytes, immutable field access | `core/packet.py`, `core/requests.py`, `core/models.py` |
| `msg-identity` | Typed identity/credential/certificate/CSR records, credential and current-state admission, pinned CA chains, live delegation, scope/ceiling/mode primitives, browser/API/OAuth and token-recovery validation, transaction-bound resource authorization | `security/authentication.py`, `certificates.py`, `policy.py`, `oauth.py` |
| `msg-storage` | Transaction-bound authority reads; SQLite/PostgreSQL writers and typed records, savepoints/rollback effects; native Git/blob content, ranges, pins and revision trees | `storage/sqlite.py`, `storage/postgres.py`, `storage/git.py` and their record/session modules |

Authentication validates service/digest/expiry, current credentials, SHA-256
bearer verifiers with constant-time comparison, actor/subject bindings, permitted
entry, custodial limits, attached certificates, archive/quarantine state and a
latched recovery generation. A changed generation stays stale until the service
is reconstructed. Missing, revoked or expired authority fails closed.

Certificate checks include pinned root bytes/public key, parent signatures and
revocation, current signing keys, scope/grant/constraint narrowing, issuance TTL,
delegation depth, the absolute CA depth limit, restricted online-CA issuance and
live source ownership/revision/credential ceilings. CSR publication checks retain
the original signed bytes, digest and possession proof. A trusted capability
registry projection is injected locally; requests cannot supply authority rules.
Only the existing network-constraint schema is implemented, not a generic schema
engine or plugin loader.

OAuth validation follows current signing parents, captured/current ceilings,
auth versions, browser sessions, refresh-family authority and custodial vault
status. MCP audience is passed from a trusted adapter, not from an envelope
field. This is **binding validation**, not an OAuth or MCP server. The helper's
operation/capability declarations are synthetic test configuration, not a
production registry adapter.

## Admission is not authorization

`Principal` has private fields and no deserializer. Request JSON cannot mint one.
`Admission::Fresh` supplies an authenticated identity only. Resource mode,
certificate-gate, traversal, membership, sharing/DM/tool and operation-specific
checks still have to be integrated before executing any operation or exposing
private data. The pure mode/scope helpers do not grant permission on their own.

`Admission::Replay` contains an already committed result and **no fresh
principal**. An old rotated token or exhausted delegated credential can only
retrieve its exact eligible result; a changed request ID, digest, revoked
successor or wrong result namespace cannot authorize a new handler execution.
Consumed token recovery has the same replay-only boundary. Recovery validation
does not issue credentials or consume a delivery record.

## Storage safety boundary

`SqliteAuthority` remains read-only. `SqliteWriter` separately opens an existing,
Python-migrated WAL database. Neither API creates or migrates a database. The
writer checks schema version, required tables/columns, primary and unique keys,
foreign keys, the one-root index and append-only audit guards. It rejects database
symlink/hard-link aliases; all processes must use the same configured file.

Read snapshots and write sessions share `AuthorityStore` queries inside their
own transaction. JSON columns remain bounded in SQL; secret vault ciphertext is
not selected. Sessions are borrowed and non-Send. Callers have no SQL, COMMIT or
ROLLBACK handle. Only a successful store-owned COMMIT can return a success value.

The Unix writer uses the same `.writer.lock`/`flock` process fence as Python,
with no-follow opens, mode 0600 on creation and a bounded acquisition deadline.
The fence outlives SQL rollback and every reverse-order rollback effect. Nested
savepoints retain successful inner effects until the outer commit; failed inner
units undo their own data/effects before the caller can recover. An ignored write
error poisons its unit rather than allowing a partial commit or an autocommit
write after SQLite has aborted the transaction.

Implemented mutation ports: resource insertion and generation-checked replacement
(including immutable creation facts, parent cycles, aliases, tags and authorization
epoch), revisions/relations, settings, result rows with subject/digest binding and
the existing 100,000-row cap, events and the existing canonical audit hash chain.
These ports are **trusted internal storage APIs**, not authorization decisions.
`save_result` does not sign receipts, redact secrets or execute handlers.

Cleanup errors preserve the primary failure and disable that writer instance.
A failed COMMIT with no live SQLite transaction is treated conservatively as
`commit_outcome_uncertain`: no destructive compensation runs, and the instance
refuses further writes. The future service owner must stop admission, inspect the
result ledger and reconcile external work before reopening. Reopening alone is
not recovery. Durable service-wide quarantine and external-work reconciliation
are not implemented by this slice.

The earlier process-kill tests cover **SQL metadata**, not a complete operation.
The content tests above additionally cover standalone blob publication. Neither
rollback callbacks nor private staging implement a durable cross-store recovery
journal: callbacks do not run after SIGKILL. Identity/OAuth workflows, outbox/lease
execution and complete operation recovery still need to be ported. PostgreSQL
metadata primitives are already implemented above. Do not introduce dual writes.

## Reproducible validation

From the repository root, with Python 3.15 and Rust 1.85.0:

```sh
python -m pip install cryptography==46.0.3 jsonschema==4.25.1 ruff==0.16.9
cargo +1.85.0 fmt --manifest-path rust/Cargo.toml --all -- --check
python -m ruff check rust/tests
python -m ruff format --check rust/tests
cargo +1.85.0 test --locked --manifest-path rust/Cargo.toml --workspace --all-targets
cargo +1.85.0 test --locked --manifest-path rust/Cargo.toml --workspace --doc
cargo +1.85.0 clippy --locked --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo +1.85.0 build --locked --manifest-path rust/Cargo.toml --workspace --examples
PYTHONPATH=src python rust/tests/test_python_parity.py \
  --binary rust/target/debug/examples/wire_check --report rust/artifacts/parity.json
PYTHONPATH=src python rust/tests/test_identity_parity.py \
  --binary rust/target/debug/examples/authority_check --report rust/artifacts/identity-parity.json
PYTHONPATH=src python rust/tests/test_storage_parity.py \
  --binary rust/target/debug/examples/writer_check --report rust/artifacts/storage-parity.json
PYTHONPATH=src python rust/tests/test_content_parity.py \
  rust/target/debug/examples/content_check --report rust/artifacts/content-debug.json
```

Repeat the build with `--release` and the same suites against
`rust/target/release/examples/`. CI performs both profiles, records exact source
SHA/toolchains, and preserves reports plus `Cargo.lock`. Source changes to Python
identity/storage/OAuth also trigger differential checks. The local offline editor
now has the same pinned compiler and Python reference dependencies; independent
cloud results must still be inspected before claiming CI acceptance.

The wire suite covers canonical floating-point boundaries, Unicode, large
integers, malformed packets and bidirectional signatures. The authority suite
creates disposable databases using the actual Python `SqliteMetadataStore`, then
compares native and Python decisions over the same records. It covers live
revocation, CA constraints, OAuth source changes, recovery, exact retries and a
real concurrent WAL snapshot test. It also checks all 4096 modes across three
classes and five permissions: **61,440 simple mode decisions**, not 61,440
independent identity scenarios. Report groups separate these from other cases.

The storage suite compares complete stored rows, canonical bytes, responses,
rollback order and unchanged schemas against the actual Python storage methods.
Separate safety/process groups cover swallowed errors, implicit rollback, failed
COMMIT, result caps, real process kills and Python/Rust fence interoperability.
Native tests additionally inject panics, cleanup failures and an uncertain commit
result. The last case is deterministic error injection, not a physical disk test.

`wire_check`, `authority_check`, `writer_check`, `pg_writer_check` and
`content_check` are local JSONL **test examples**, never HTTP or MCP endpoints. They use only deterministic public test keys and disposable data.
The writer helper deliberately includes unauthenticated storage orchestration
(`once`) and failure/checkpoint controls only for disposable tests. It is not a
production executor and must never be exposed as an endpoint. These examples
do not execute business handlers, make network calls, launch Python from
Rust, or access production credentials. The Rust crates forbid unsafe code;
cryptographic and database primitives are delegated to pinned libraries.

## Explicit compatibility and cutover gates

- Parser limit: 1 MiB/64 levels. Model timestamps currently accept RFC3339 UTC
  `Z` forms; Python accepts additional aliases. Model version/depth fields have
  fixed-width Rust limits, unlike Python arbitrary integers. These need an
  explicit compatibility/version decision before cutover.
- Credential JSON must contain required nullable fields. Malformed typed records
  use a unified `invalid_identity_record` code instead of Python's field-specific
  decoder errors. Corrupt result body/namespace mismatches are explicitly rejected.
- Ancestor traversal is capped at 1024, certificate traversal at 32 with a shared
  4096-work budget, and OAuth source chains at 32. Cycles/oversized restored
  authority fail closed. These defensive limits are not silently described as
  accepting every Python input.
- Dalek strict verification rejects weak-key forgeries. Historical key/signature
  compatibility still needs characterization before routing production traffic.
- Storage row decoding uses `invalid_storage_record` rather than every Python
  field-specific error. Domain validation (notably Unicode tag normalization) remains pending.
  Resource policy and PostgreSQL hexadecimal aliases are implemented above. Native storage takes
  canonical IDs/validated domain records; its DTOs are not public request schemas.
  The stricter poisoned-write and malformed-result checks are explicit safety
  differences, not assertions of identical behavior for every Python input.
- Operation handlers, mutation-side identity/OAuth
  issuance/refresh/recovery, encrypted-key wrapping and remaining content stores,
  native `serve`/HTTP/MCP, `worker`, packaging and deployment remain pending.
- No RSS, memory-reduction or throughput result is claimed. Benchmark equivalent
  functionality, data, concurrency and complete process trees before comparison.

## Next implementation slices

1. Integrate the real operation/capability registry and complete resource
   authorization, including private resources, sharing, memberships and tools.
2. Complete remaining workflow mutations, LFS and durable cross-store recovery;
   connect the writer to the real authorized executor, receipt signing and secret
   delivery. Retain Python migration/administration scripts. Extend the current
   metadata crash/retry checks to complete operation effects.
3. Integrate a bounded native executor and HTTP/MCP adapters, then worker
   claiming/leases/retries and side effects. Empty loops or Python subprocesses
   do not count as a server port.
4. Rehearse isolated deployments and rollback, then measure equivalent workloads.
   Keep the current Python service until explicit cutover acceptance.

## Coordination

Repository `AGENTS.md`, entry skill and deployed `/AGENTS.md` were read. No
selected MSG signing identity was available in the editor, so no authorized
private coordination thread could be created. No new production identity or
public coordination post was created. Work is recorded in the migration
branch/PR; use an existing authorized identity when private coordination resumes.
The temporary offline-input export workflow is removed by the fifth slice.
No selected production MSG identity was invented or registered for testing.
