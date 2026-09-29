# Hosting read-only runtime — #161

Base: `261c8816cd581deaf69fc0c810b8fa6dad5ead5e`, tree `6171f6041ffd0addaf72eadd35acf562f478281f` (#167 merged).
Branch: `fix/hosting-readonly-20260929`; PR #173; coordination #83.

## Scope and authority

Only #161: independent hosting composition, read-only metadata/content, public trust, deployment identity and regression evidence. Keep current same-origin/CSP rules. Do not change signing bytes, IDs, grants, operation versions or main/worker behavior. #80/#84 production acceptance remains separate. Capability matrix and operator guidance: `HOSTING_RUNTIME.md`.

Read the current design and completed-contract archive on 2026-09-29. Initial source: #167 artifact `10999494535` (independently verified SHA256 `cff2bbc5beb0575f79e1eaf069494a0cbdcfe5862b64c15401f974a1791340f0`); source tree matches this base.

## Cloud evidence

- `0942251`: first tests/workflow. Run `36547254507` failed at Docker initialization because runner health-command parsing requires double quotes. No test execution; not RED evidence.
- `6d24ee847c2e4c0349c71ce667d45b4ac04393f1`: valid RED run `36547778158`: **17 tests, 11 failures, 6 passed, 0 errors, 0 skipped**. Ten missing-runtime failures and one existing shared-user/read-write unit failure. Old hosting/CLI tests passed. Artifact `11023785174`, independently verified SHA256 `c4f67c186d03cd57df2a85668dcd5487ac5f261a91f913dfb047432eb6b7f2c0`; merge checkout `90744666cc37d156366041d63ee4b7d33a4e26eb`, tree `201784072a47b7b036e400b769c9e9dab225cd89`, Python 3.15.0rc2.
- `20399c0eacffece853d2d0e2035986e30b0636a4`: permission tests added before implementation. Valid RED run `36548533968`: **20 tests, 14 failures, 6 passed, 0 errors, 0 skipped**. Three additional failures are the missing `group_read` option. Artifact `11023512357`, independently verified SHA256 `33ff93271408f4e2006891154bf4621aa660dce6c26324b9945fdc4f54faac60`; checkout `475335d453cb3be925c6ddf5ce48c0769b83aaf1`, tree `2763aed9a71f7cefa6b68bc3214a15c3d43aaa36`.

## Implemented; GREEN/full verification pending

- Shared ordered Registry installer; HostingRuntime retains contracts but replaces every handler/requirement closure, including plugin manifests. No full Application, issuer, executor, private service key, cursor MAC or outbound sender.
- Read-only PostgreSQL composition opens with initialization disabled, uses real READ ONLY transactions and validates the independent login's actual grants. Content reader binds the canonical streaming implementations without a writer/staging instance.
- Startup verifies public trust, current authority and exact release source records. It never creates missing storage, migrates schema or synchronizes release rules. Shared recovery marker plus per-request database quarantine remain fail-closed.
- Dedicated OS/DB configuration and operator-only preparation script. Opt-in writer `hosting.content_group_read=true` publishes group-readable/non-writable Git/index/CAS files under umask 0077; existing private permissions are never silently rewritten.
- Daemon chooses hosting before normal Application construction. Existing publication/CSP test now uses the read-only fixture without deleting assertions. New assertions use the existing canonical error `read_only_transaction` and existing forbidden-host 403 status, rather than changing those contracts.
- Added daemon composition regression; dedicated workflow also executes the established publication-origin regression.

All project execution runs in GitHub Actions. Local work is source text/Git/AST inspection and artifact parsing only. No production probes, deployments, real Root/PIN/key reads, outbound delivery, destructive backups or restore promotion. Do not close #161 before inspecting GREEN and complete four-shard evidence against the current head/tree; reconcile live main before merge.
