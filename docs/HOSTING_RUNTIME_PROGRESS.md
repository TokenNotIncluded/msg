# Hosting read-only runtime — #161

Base `261c8816cd581deaf69fc0c810b8fa6dad5ead5e`, tree `6171f6041ffd0addaf72eadd35acf562f478281f` (#167). Branch `fix/hosting-readonly-20260929`, PR #173; coordination #83.

## Scope and implementation

Independent HostingRuntime, canonical Registry metadata without business callbacks, original authentication/authorization, real SELECT-only PostgreSQL login, no initialization/migration/write execution, read-only canonical Git/CAS streaming, public trust/recovery gates and dedicated OS/DB deployment. Explicit content-group sharing preserves future publication readability without exposing staging or changing default private permissions. Details: `HOSTING_RUNTIME.md`.

Keep current same-origin/CSP, signing bytes, IDs, grants and operation versions. #80/#84 production acceptance is separate. Read the authoritative design/completed archive on 2026-09-29. Baseline artifact10999494535 independently verified SHA256 `cff2bbc5beb0575f79e1eaf069494a0cbdcfe5862b64c15401f974a1791340f0`; source tree matches base.

## Independently verified hosting evidence

| Head | Run / artifact | JUnit | ZIP SHA256 |
|---|---|---|---|
| `0942251` | 36547254507 / none | Docker setup failed; NOT RED | none |
| `6d24ee847c2e4c0349c71ce667d45b4ac04393f1` | 36547778158 / 11023785174 | 17: 11 failed, 6 passed | `c4f67c186d03cd57df2a85668dcd5487ac5f261a91f913dfb047432eb6b7f2c0` |
| `20399c0eacffece853d2d0e2035986e30b0636a4` | 36548533968 / 11023512357 | 20: 14 failed, 6 passed | `33ff93271408f4e2006891154bf4621aa660dce6c26324b9945fdc4f54faac60` |
| `246f0fd83f41185dde4e796d0f96f1cca8db7934` | 36549829088 / 11024435325 | 22: 8 failed, 14 passed | `f308a1c1dd112339cd6fb6912350c4f63f9964e6e4bd79acc37bd739c9789fe9` |
| `df7efdbaa637e7f316d0eab1e0ce3dc156f83954` | 36550574815 / 11024546594 | 22 passed | `6dd107a22546b392c6ce136d1eab61f9c7927c387731e73d73a4cbb9add2fd9f` |
| `6703f3de41e988977be09e509c91fc30a11738b1` | 36551192429 / 11025490714 | 35: 4 failed, 31 passed | `6543b5326faab8f1352328058279faf35e4a3bec8cee65324fc0a0b9f1852e3e` |
| `2b97ee9a3a3e7b01fbd60f7b19c84d3578ab47b7` | 36551916795 / 11024567848 | **35 passed** | `cdc5f7fc9c68de649c58098e251d9a2c84b0e29d3db1de6da30f13da0d4af281` |

Every JUnit above has zero errors/skips. Source manifests were verified against exact heads/trees. Latest GREEN checkout `ff7f2d95c50522b58194b5a4aeaa7bba4227429a`, tree `75cf741527a4d62757b8a495ef34d0ce9175c009`, Python3.15.0rc2, attempt1. These are isolated source acceptance results, not deployment evidence.

Initial implementation failures shared PostgreSQL predicate-evaluation ordering: sequence privilege checks received index OIDs. `df7efd` resolves sequences first. Extended RED found three real readiness gaps (missing version table, wrong version, missing required SELECT) and one CI sudoers launch failure before application code. `2b97ee9` verifies all required tables/grants and version1; the UID test uses trusted `setpriv` to drop to UID65534 and clear supplementary groups before Python. It does not alter sudoers or run the application as root. All assertions remain; the actual different-UID read/new-publication/private-key-denial/write-denial test passed in 3.914 seconds.

## Parallel convergence

#174 independently overlapped #161. Coordination: #83 comment5887561813 and #174 comments5887616225/5887713667. Only one runtime implementation should merge. Its independent-UID/failure-containment tests were retained from head `eafea15af0b8ff26c0208d5c0fcc039b4d32fb56` with attribution in `tests/test_hosting_runtime_isolation.py`, then extended with exact version/grant/sequence/secret-table/quarantine-latch checks. The 35-case GREEN above includes these tests. No old assertion was deleted; no default ACL or post-publication chmod hides new-file permissions.

## Token-delivery integration (#162, #172, #175)

#172 merged into the old executor feature branch, not main. Its merge commit is `b0b51205c8273c2d89bf99c7facf9e56767bc8d2`; comparison with main261c8816 is ahead4/behind1 with exactly its six changed files. PR #175 fixes the target using an independent promotion branch, without overwriting either original source branch.

Promotion focused run `36551495692`, artifact `11024782079` independently verified: **122 passed, zero failures/errors/skips**, ZIP SHA256 `9079f195d8d49568fe502c140ec7e2f05412bf751852ef180d74e681610a3312`. Manifest headb0b51205, checkout `8c86062ba7c60f63d7a4824bce66db1db0f0001c`, tree `ebe1facca432803854d0c04aaa93519a589ff93f`, Python3.15.0rc2/attempt1. Read the complete token owner, authentication change and all seven added boundary tests. Original TDD evidence remains in `TOKEN_DELIVERY_BOUNDARY_PROGRESS.md`.

This integration preserves both parents (hosting2b97 and tokenb0b512), reuses all five non-Application token blobs unchanged and combines only the Application wiring: shared Registry installer/group-read construction plus the single TokenDelivery owner. No business rule/operation version is rewritten. Both focused suites and full four-shard/node-ID/conformance/build/recovery gates must run on this combined head before merge. Focused GREEN on either separate parent is not substituted for that gate.

Full runs36551916794 (hosting) and36551495771 (promotion) were still running at this note update. Final verification/merge evidence will be attached to #173/#175; do not close #161/#162 from this note alone. Merge serially, refresh actual main/tree, and retain #80/#84 production blockers.

All project execution runs in GitHub Actions. Local work is source text/Git/AST inspection and artifact parsing only. No production probes, deployments, Root/PIN/private-key access, real outbound delivery, funds, destructive backup operations or restore promotion.
