# Hosting read-only runtime — #161

Base: `261c8816cd581deaf69fc0c810b8fa6dad5ead5e`, tree `6171f6041ffd0addaf72eadd35acf562f478281f` (#167 merged).
Branch: `fix/hosting-readonly-20260929`. Coordination: #83.

## Scope and authority

Only #161: independent hosting process composition, read-only metadata/content, necessary public trust, deployment identity and regression evidence. Keep the current same-origin/CSP rules. Do not change signing bytes, IDs, grants, operation versions or the main/worker behavior. #80/#84 production acceptance remains separate.

Read the current design and completed-contract archive on 2026-09-29. Existing source is the #167 build artifact `10999494535` (SHA256 `cff2bbc5beb0575f79e1eaf069494a0cbdcfe5862b64c15401f974a1791340f0`); its checkout tree matches this base. No production probes or key reads.

## Execution log

1. Added failing acceptance tests before implementation: no Application/signers/installer; actual unreadable service-key directory; SELECT-only PostgreSQL account; current certificate/preview checks; HEAD/Range/304; recovery gates; stale/missing prerequisites; distinct systemd identity.
2. Added `hosting-readonly.yml`: real PostgreSQL 16/Git/age/Python 3.15, source identity + JUnit artifacts. Keep prior hosting/CLI regressions in the same job; full existing four-shard CI is unchanged.
3. RED run pending. No test result or implementation completion claimed yet.

## Next gates

- Reuse one Registry installer and the existing authentication/authorization; hosting retains contracts but no captured business handlers or Application pointer.
- Read-only storage opens without migration, rule publication, directory creation or writer keys. Use a separate OS/DB identity, not only a wrapper flag.
- Retain signed private previews, immediate revocation, quarantine, streaming/range and CSP.
- Inspect RED then GREEN artifacts against head/tree, complete full cloud CI and reconcile current main before merge.

All project execution runs in GitHub Actions. Local work is source text/Git inspection and artifact parsing only. No deployment, real Root/PIN, external delivery, destructive backup operation or restore promotion.
