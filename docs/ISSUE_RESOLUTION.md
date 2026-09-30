# Open issue resolution ledger · 2026-10-01

Audit baseline: main `5688012` after PR #206. Of the 18 originally open issues,
#200, #202 and #72 have been resolved and closed; 15 remain open. This ledger
keeps the original scope rather than turning green tests into blanket acceptance.

## Remaining requirements

| Issue | Current evidence | Work needed before closure |
| --- | --- | --- |
| [#64](https://github.com/TokenNotIncluded/msg.lmm.best/issues/64) ingress secrets | Actual nginx configuration/listeners read over authorized SSH; writer binds loopback; site logging off; isolated nginx/request-target regressions in full CI | Global/pre-Host shared nginx policy, all default vhosts and upstream/collector inventory, controlled whole-chain sentinel/error evidence. Site-only logging suppression is insufficient. |
| [#65](https://github.com/TokenNotIncluded/msg.lmm.best/issues/65) legacy ledger | Read-only target inventory: typed schema, zero ledger rows/legacy escrows; fixed-old-source fixture and migration rollback tests exist | Protected real legacy snapshot with provenance/freeze point; isolated migration, exact receipts/rows/sequences, interruption/retry/concurrency and rollback. Current empty ledger is not an old snapshot. |
| [#68](https://github.com/TokenNotIncluded/msg.lmm.best/issues/68) historical ciphertext | Existing custodial inventory/ACK/rewrap/retirement tests and root-signed backup-retirement verification | Match every original scenario to its assertion; real retained ciphertext decrypted by its authorized client; independently bounded backup-retirement facts. No key destruction is inferred. |
| [#69](https://github.com/TokenNotIncluded/msg.lmm.best/issues/69) revocation after restore | Proof/current-authority reconcile/promotion code exists; installed restore checks retain quarantine after marker removal | Full scenario-to-assertion review; actual independently current checkpoint outside the rollback set, protected snapshot and controlled-console promotion. Do not repeat the obsolete claim that promotion has no implementation. |
| [#70](https://github.com/TokenNotIncluded/msg.lmm.best/issues/70) Root/CA governance | Target inventory found Root and online CA with finite signed policies; no trust changes; latest-operation gaps identified | Exact signed grants/scopes/constraints/depth/expiry inventory and approved per-chain reissue/revoke plan; full lifecycle matrix and real VT/serial success. SSH opt-ins do not prove physical-console success. |
| [#76](https://github.com/TokenNotIncluded/msg.lmm.best/issues/76) authorization | Source/adapter, cache, attachment, concurrent revocation and quarantine regression families pass in the complete run | Original source × representation × invalidation requirements need explicit assertion mapping, including independent legal-source positives and field/count/digest privacy. |
| [#77](https://github.com/TokenNotIncluded/msg.lmm.best/issues/77) personal/Legacy/honors | Existing signed personal content, Legacy and R1–R5 test families in full run | Map every history/key/invalidation/challenge/CLI/TUI requirement to concrete assertions; separate protocol completion from identity/trust claims. |
| [#78](https://github.com/TokenNotIncluded/msg.lmm.best/issues/78) read/search/Sync | `READ_ENTRY_ACCEPTANCE.md` maps supported semantics; PR #206 restores immutable v1 dictionary/schema and version binding | Complete original budget/pagination/filter/output/current-authority mapping. Representative dispatcher equivalence does not by itself prove every predicate boundary. |
| [#79](https://github.com/TokenNotIncluded/msg.lmm.best/issues/79) content/Revision | File, patch/rebase/batch, signed Revision and physical-failure tests in complete run | Map all original mutation/metadata/scope/owner/group/generation/template/link/limit requirements; exact filesystem orphan behavior must remain distinct from DB atomicity. |
| [#80](https://github.com/TokenNotIncluded/msg.lmm.best/issues/80) Transfer/Git/LFS/hosting | Real installed OpenSSH/Git and backup/recovery checks; Transfer/LFS/hosting regressions | Full clause matrix plus target shared-volume/multi-instance budget, disk-limit, power-loss/GC/restore durability. Single-process locks are not multi-instance evidence. |
| [#81](https://github.com/TokenNotIncluded/msg.lmm.best/issues/81) events/notifications/collaboration | Current-attempt/deadline fences and controlled isolated sender tests | Event/state/privacy/current-permission assertion matrix; controlled target SMTP/Webhook success/failure/uncertain/retry/recovery evidence. No arbitrary production recipients. |
| [#82](https://github.com/TokenNotIncluded/msg.lmm.best/issues/82) clients/tools/SSH/RSS | RSS issue resolved; XDG credentials/native layout, environment-based TUI locales, installed-client/SSH and sandbox regressions | Remaining view/output/retry/explicit-write clause map; target application SSH/PAM and bwrap resource/network-limit matrix. Administrator SSH port 22 is not the application's SSH endpoint. |
| [#83](https://github.com/TokenNotIncluded/msg.lmm.best/issues/83) full design acceptance | Immutable published read contract repaired; exact 2,851+8 evidence gate; finite design inventory exists | All 564 design obligations need concrete positive/negative/concurrency/recovery assertions where applicable. Inventory navigation entries alone are not assertion coverage. Refresh outdated diagnostic-gap entries against actual main. |
| [#84](https://github.com/TokenNotIncluded/msg.lmm.best/issues/84) production/recovery | Native 0.2.0 deployed; real package/health/doctor/selftest checks; protected release backup and rollback package retained | Legacy snapshot consistency and independently current authority; actual target capacity/resource exhaustion/shared-topology recovery, cutover and stopping criteria; latest CA transition remains unresolved. |
| [#85](https://github.com/TokenNotIncluded/msg.lmm.best/issues/85) architecture/test debt | Complete current CI, shared protected ledger/market owners and effect fences | Measure current duplication/hot paths and trace all convergence invariants; keep published versions/signatures and history. Green Ruff or smaller files are not architectural acceptance. |

## Concrete fixes in this pass

PR #206 has been merged with all checks passing. It repairs published schema
compatibility, short-code version preservation, strict HTTP boundary fixtures and
installed restore-marker discovery without relaxing quarantine assertions.

The remaining hardcoded `/tmp` in the PostgreSQL pytest fixture has been removed;
`tempfile` now chooses the platform/environment directory, including `TMPDIR`.
The fixture still creates an isolated cluster and tears it down after the run.

Read-only CA preflight now includes exact signed use grants and issuance policy,
scopes/constraints, issuer/service, authority sources, validity, delegation depth
and certificate digest. The report remains blocked until independent field
requirements are fulfilled. A database policy export does not validate its own
external provenance or authorize reissue. Store complete reports privately:
resource scopes can reveal non-public identifiers. Do not paste raw policies,
configuration, logs or backup contents into public GitHub issues.

## Verified source evidence

[PR #206](https://github.com/TokenNotIncluded/msg.lmm.best/pull/206) head
`ab301ca4b770f5917f2c57c2a09aa2313ef543b9`, tree
`e7e75a0a38fcf2d624c5f67bdf64a1e70c2184ca`, matches merged main `5688012`.
[Complete run 36753933957](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36753933957)
contains eight digest-verified shard artifacts; `scripts/ci_shards.py check 8`
independently confirms the exact 2,851-node disjoint union. Conformance has
8 tests and no failure/error/skipped elements. Current-head installed rehearsal
and specialized checks also passed. Later patches need separate validation.

For #72, `BOUNTY_ACCEPTANCE.md` maps the original clauses to concrete assertions.
Its 30 explicit named nodes are all in that exact run, with the whole-file
contracts and installed official market fixture. The issue's isolated funding
and notification scope is fulfilled; separate field gates stay in #70/#81/#84.

## Operator materials still needed

Provide protected snapshot/checkpoint/retirement locations and source timestamps,
or physical/serial console access, rather than transmitting secrets in chat.
Without these materials, code work and isolated verification can continue, but
real-snapshot/console/retirement requirements cannot honestly be marked complete.

A blanket Root rotation would invalidate existing chains. The latest rename and
homepage-v4 changes require an explicit compatible governance decision before
deployment. Current source, deployed source and signed authority are reported
separately; none of these issues authorizes inventing attestation evidence.
