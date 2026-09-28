# C read-integrity evidence and remaining acceptance

Refs #76, #78, #82, #83. This is a narrow cache correctness/security increment,
not completion of the authorization/read/client matrices or production acceptance.
Base: `67401759a853c69fc5cb234982e1f7020b650542` (includes #114).

## Implemented contract

`discovery.get` now recognizes the digest of either exact public representation:
full JSON or the existing compact operation-result JSON. Compact maps omit null
properties; arrays keep null elements. The current authorized projection is always
computed first. Cache hints do not grant authority, skip current revocation, change
fields, modify persisted Revision/signature bytes, or introduce another validator.
The matching representation's digest is returned in the `not_modified` response.
No operation version, short code, CA grant or default configuration is changed.

## Reproducible evidence

- `tests/test_read_projection_cache_unit.py`: 12 cases covering both exact
  representations, nested null maps, array nulls, wrong/narrowed/different-object
  digests and current authorization failure. Python 3.13.5 before the fix: 4 failed,
  8 passed. After the fix: 12 passed. These handler tests mock the projection and
  are explicitly not database evidence.
- `tests/test_read_projection_cache_transports.py`: six actual signed-client paths
  (HTTP, PathGET, GraphQL, MCP HTTP, CLI subprocess, MCP stdio subprocess) against
  disposable PostgreSQL and the real HTTP application. For default current,
  historical, metadata and history projections: warm, conditional and stale reads;
  then explicit revoke and denial with the same clients and cache validators.
  Complete business rows and exact content-file hashes are unchanged by reads.
  Content mutation, pending-job notification, SMTP, Webhook and sandbox invocation
  spies must remain unused. They forbid external effects rather than simulate success.
- Combined local command: `python -m pytest -q
  tests/test_read_projection_cache_unit.py
  tests/test_read_projection_cache_transports.py`: **18 passed, 0 failed, 0 skipped**,
  Python 3.15.0rc2 / PostgreSQL 17.11 / Valkey 8.1.1; 87.27 seconds. The disposable
  PostgreSQL cluster uses UTF-8. This is not the target PostgreSQL 16 / Valkey 9 CI.
- The exact final-head four-shard core suite, JUnit node-ID gate, conformance and
  wheel/sdist results must be recorded in the PR/#83 after completion. A running
  job or a previous head's result is not accepted. No skip/xfail, allowlist or
  timeout increase is introduced by this patch.

## Remaining C scope

#76: full source/HEAD/Range/attachment/LinkSet/cache/error privacy and independent
legal-source matrix; joint quarantine evidence with A. #78: all nested pagination,
long read/Sync, complex query and complete Search/entry equivalence acceptance.
#77/#79: personal/Legacy/honor and text/Revision/editing matrices remain open;
this patch changes none of their signed-history semantics. #82: Following, explicit
writes, full output formats and real sshd/bwrap/network-negative acceptance remain
open; six read clients do not substitute for those tests. #83/#85: maintain the
exact evidence gate and module boundaries; no production evidence is asserted.

No deployment, production Root/PIN/key/funding/recovery/deletion, live email or
network capability enablement was performed. Temporary offline-toolchain/patch
transport workflows are confined to an auxiliary branch, not this change.
