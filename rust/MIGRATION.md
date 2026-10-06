# Native migration acceptance checklist

**Completed / remaining: 5 / 6.** This counts the fixed eleven packages below,
not a percentage of effort, code lines, operations or production readiness.
A foundation package is not the corresponding end-to-end server feature.
Keep the IDs stable in later progress reports; split/redefine one only explicitly.

| ID | Package and completion boundary | Status |
|---|---|---|
| M01 | Strict canonical wire/packet/crypto primitives and Python parity | Implemented |
| M02 | Transaction-bound identity admission, certificate and resource-policy primitives; not issuance handlers | Implemented |
| M03 | Native existing-schema SQLite metadata sessions, writers and process fencing | Implemented |
| M04 | Native existing-schema PostgreSQL metadata sessions, writers and process fencing | Implemented |
| M05 | Native Git/blob content, ranges, pins and revision graphs; valid-data interoperability | Implemented in slice 5 |
| M06 | LFS shared-object lifecycle and durable metadata/content recovery; no ambiguous-commit data loss | Remaining |
| M07 | Real operation/capability registry, authorized executor, business handlers, receipts and secret delivery | Remaining |
| M08 | Native identity/OAuth/browser-session issuance, rotation, refresh, revocation and encrypted-key wrapping | Remaining |
| M09 | Native `msgd serve`, HTTP/MCP/GET and other supported transports with current contracts | Remaining |
| M10 | Native `msgd worker`, leases/outbox, retries, recovery fences and actual side effects | Remaining |
| M11 | Installable native daemon, isolated cutover/rollback rehearsal, full end-to-end acceptance and equivalent benchmarks | Remaining |

Python retains `msg` CLI, administrator/migration scripts and developer tooling.
There is still no native production daemon in this PR. Root signing remains local
only. Do not merge/deploy solely because these primitive suites pass.

## Slice 5 acceptance and boundaries

- 14 new native content tests; current workspace: 46 unit tests and 5 doc tests.
- 170 Python/Rust content checks per debug/release profile. These include actual
  shared storage, 64 KiB boundary ranges, hard-link retention, canonical revision
  manifests and ancestry, pin interoperability, corrupted objects, ignored Git
  environment/hooks, and process kills before/after publication.
- Existing wire, identity, SQLite writer and SQLite authorization differential
  suites remain enabled: respectively 18,447 / 61,734 / 71 / 878 checks. The large
  identity count includes the documented simple mode matrix, not independent
  end-to-end identity scenarios. PostgreSQL suites remain mandatory in cloud CI.
- `cargo fmt --check`, Ruff check/format, Clippy `-D warnings` and locked builds
  remain mandatory. No warning suppression or production Python changes were
  used to obtain a native pass.
- CI writes source commit/tree, compiler version, lockfile and per-profile JSON
  reports to its artifact. Inspect the actual run for acceptance; a workflow
  trigger or a previous commit's green status is not evidence for a new tree.

Stricter invalid-data behavior is intentional: corrupt content is not delivered,
paths/indices are bounded and checked, reused revision IDs are immutable, and
native commit timestamps are deterministic. This is valid-data compatibility,
not a claim to reproduce every Python error or accept every malformed old record.
Directories are trusted service-owned configuration, not caller-selected paths
or an openat capability sandbox. Full reads use verified disk snapshots; request
admission must account for their disk/I/O/concurrency costs.

No partial operation may destroy externally published data merely because an SQL
COMMIT acknowledgement was lost. M06/M07 must integrate explicit reconciliation
with the existing writer fence and ledger before serving real requests. Staging
left by SIGKILL is retained intentionally; this slice does not add unsafe GC.

## Pre-existing full-regression blocker

Before this slice, Rewrite contracts run **37299218496**, source head
`de810d15da747b7be34b64f4e2324e5f5e7da6f7`, failed all eight started test shards.
Their JUnit artifacts were inspected (4,759 test entries): **32 failures/errors**,
of which **29** end in `recovery_schema_unsupported`. The other three are:

| Test | Existing failure location |
|---|---|
| `test_default_build_checks_packaged_publication_snapshot` | `tests/test_dictionary.py:178` |
| `test_universe_account_hint_is_minimal_current_identity_without_private_reads` | `tests/test_universe.py:51` |
| `test_accepts_postgres_and_optional_valkey_urls_without_revealing_secrets` | `tests/test_pg_config.py:26` |

These are observed baseline failures, not fixed by changing native content code.
Do not label a new failure as pre-existing without matching its actual evidence.
The full regression/cutover gate remains open independently of native conformance.

## Coordination and evidence

Read repository `AGENTS.md`, the entry skill and deployed `/AGENTS.md` before this
slice. The editor has no selected local MSG signing identity; no authorized
private coordination thread could be created. Use the existing draft PR #235 and
this checklist as the fallback. No production account, public coordination post,
production data write or deployment was created. The temporary public-toolchain
export workflow is removed from the final source tree.
