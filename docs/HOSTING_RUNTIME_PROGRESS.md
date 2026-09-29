# Hosting read-only runtime — #161

Base: `261c8816cd581deaf69fc0c810b8fa6dad5ead5e`, tree `6171f6041ffd0addaf72eadd35acf562f478281f` (#167 merged).
Branch: `fix/hosting-readonly-20260929`; PR #173; coordination #83.

## Scope and authority

Only #161: independent hosting process composition, read-only metadata/content, necessary public trust, deployment identity and regression evidence. Keep the current same-origin/CSP rules. Do not change signing bytes, IDs, grants, operation versions or main/worker behavior. #80/#84 production acceptance remains separate.

Read the current design and completed-contract archive on 2026-09-29. Initial source: #167 build artifact `10999494535` (SHA256 `cff2bbc5beb0575f79e1eaf069494a0cbdcfe5862b64c15401f974a1791340f0`), independently hashed; its source tree matches this base. No production probes or key reads.

## Cloud evidence

- `0942251`: first acceptance tests + dedicated workflow. Run `36547254507` failed at Docker initialization because the runner parsed single quotes in `--health-cmd` incorrectly. No tests ran; this is not RED evidence.
- `6d24ee847c2e4c0349c71ce667d45b4ac04393f1`: corrected runner quoting. Valid RED run `36547778158`: **17 tests, 11 failures, 6 passed, 0 errors, 0 skipped**. Ten failures are the missing read-only runtime and one is the existing shared-user/read-write unit. Existing hosting/CLI regressions passed.
- RED artifact `11023785174`, independently verified SHA256 `c4f67c186d03cd57df2a85668dcd5487ac5f261a91f913dfb047432eb6b7f2c0`. Manifest: head `6d24ee8`, merge checkout `90744666cc37d156366041d63ee4b7d33a4e26eb`, tree `201784072a47b7b036e400b769c9e9dab225cd89`, Python `3.15.0rc2`.
- Added separate permission acceptance before implementation: newly published text/CAS/index files must be group-readable but never group-writable; staging remains private; the default remains private; enabling sharing must refuse rather than chmod an existing installation silently. Cloud run pending.

## Implementation in progress / remaining gates

- Reuse one Registry installer and existing authentication/authorization; hosting retains metadata but no captured business handlers or Application pointer.
- Open read-only storage without migration, rule publication, directory creation or writer keys. Separate OS and DB accounts; verify actual privileges, not just a wrapper flag.
- Opt-in content read group needs real publication modes, not a unit file that cannot read future files. Offline operator preparation only; no automatic historical chmod.
- Retain signed private previews, current revocation, quarantine, streaming/range and CSP.
- Inspect GREEN artifacts against head/tree, complete full cloud CI and reconcile current main before merge. No implementation completion or GREEN result claimed yet.

All project execution runs in GitHub Actions. Local work is source text/Git inspection and artifact parsing only. No deployment, real Root/PIN, external delivery, destructive backup operation or restore promotion.
