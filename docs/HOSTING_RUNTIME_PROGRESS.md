# Hosting read-only runtime — #161

Base `261c8816cd581deaf69fc0c810b8fa6dad5ead5e`, tree `6171f6041ffd0addaf72eadd35acf562f478281f` (#167). Branch `fix/hosting-readonly-20260929`, PR #173; coordination #83.

## Scope and implementation

Independent HostingRuntime, canonical Registry metadata without business callbacks, original authentication/authorization, real SELECT-only PostgreSQL login, no initialization/migration/write execution, read-only canonical Git/CAS streaming, public trust/recovery gates and dedicated OS/DB deployment. Explicit content-group sharing preserves future publication readability without exposing staging or changing default private permissions. Details: `HOSTING_RUNTIME.md`.

Keep current same-origin/CSP, signing bytes, IDs, grants and operation versions. #80/#84 production acceptance is separate. Read the authoritative design/completed archive on 2026-09-29. Baseline source artifact10999494535 has independently verified SHA256 `cff2bbc5beb0575f79e1eaf069494a0cbdcfe5862b64c15401f974a1791340f0`; source tree matches base.

## Verified cloud evidence

| Head | Run / artifact | JUnit | Independent ZIP SHA256 |
|---|---|---|---|
| `0942251` | 36547254507 / none | Docker setup failed; NOT RED | none |
| `6d24ee847c2e4c0349c71ce667d45b4ac04393f1` | 36547778158 / 11023785174 | 17: 11 failed, 6 passed, 0 errors/skips | `c4f67c186d03cd57df2a85668dcd5487ac5f261a91f913dfb047432eb6b7f2c0` |
| `20399c0eacffece853d2d0e2035986e30b0636a4` | 36548533968 / 11023512357 | 20: 14 failed, 6 passed, 0 errors/skips | `33ff93271408f4e2006891154bf4621aa660dce6c26324b9945fdc4f54faac60` |
| `246f0fd83f41185dde4e796d0f96f1cca8db7934` | 36549829088 / 11024435325 | 22: 8 failed, 14 passed, 0 errors/skips | `f308a1c1dd112339cd6fb6912350c4f63f9964e6e4bd79acc37bd739c9789fe9` |
| `df7efdbaa637e7f316d0eab1e0ce3dc156f83954` | 36550574815 / 11024546594 | **22 passed, 0 failures/errors/skips** | `6dd107a22546b392c6ce136d1eab61f9c7927c387731e73d73a4cbb9add2fd9f` |

Source manifests checked against each head/tree. First implementation tree0941aa2, checkout05e7b3b; GREEN tree `d974b049886a93e3cf784cba251231fcb88e6ac5`, checkout `f976bb8bbe91ca4afd88473334b9db2940dcf07b`, Python3.15.0rc2, attempt1. Initial eight implementation failures shared a PostgreSQL evaluation-order bug: sequence privilege checks received index OIDs. `df7efd` resolves sequence OIDs before privilege calls; no assertion removed or weakened. Existing error name `read_only_transaction` and Host403 remain unchanged.

## Parallel convergence and remaining acceptance

Discovered overlapping #174 after implementation; coordinated in #83 comments5887561813 and #174 comment5887616225. Do not merge both runtime implementations. Preserve #174's independent UID and failure-containment acceptance, adapted from head `eafea15af0b8ff26c0208d5c0fcc039b4d32fb56` into `tests/test_hosting_runtime_isolation.py`.

The retained tests check post-revocation HEAD/Range/304, cached quarantine and dangling markers, missing schema, excess DB privileges, and an actual UID65534 reader of fresh text/binary publications. Adaptations use the #173 runtime/error names, explicit group-read opt-in and a disposable libpq service file. No default ACL/post-publication chmod may hide unreadable new files. Added wrong schema version, missing SELECT, sequence/secret-table privileges and latched quarantine cases. This test-only extension is not yet verified; required-schema checks may need correction before GREEN.

Full four-shard/gate/conformance/build run36550574826 and ancillary boundary/recovery runs still require inspection; focused GREEN is not final completion. Reconcile current main and both PR discussions before merge; do not overwrite #155/#168/#170/#171/#172. No #161 closure yet.

All project execution runs in GitHub Actions. Local work is source text/Git/AST inspection and artifact parsing only. No production probes, deployments, Root/PIN/private-key access, real outbound delivery, funds, destructive backup operations or restore promotion.
