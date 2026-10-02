# Open issue resolution ledger · 2026-10-02

Verified production baseline before this iteration (2026-10-02): native server
`msgd 0.2.14-20261002.33`, source `8e859f9750c901ba4f5b3f2ec4f1294d93280205`;
both services active and package files intact. Current delivery must be verified
against `/usr/share/doc/msgd/build.json` rather than historical version strings.
PyPI publication and the client installer are independent delivery paths; this
iteration does not establish a new PyPI release. Instance and ingress-alias code
is present, but existing production directories have not migrated and target
DNS/TLS/proxy changes are not inferred from source changes.

The original acceptance ledger below retains its historical evidence and scope.
New feature delivery does not establish every older production/recovery gate.

## Recent issue updates

| Issue | Current state / remaining acceptance |
| --- | --- |
| #210 SSH Root money | Deployed; public ledger confirms 10000 MSG Root-to-bank and 1000 MSG bank-to-xx. Exact SSH channel/PIN/cancellation evidence remains separate. |
| #211 native package name | Resolved: installed package is `msgd`; Python distribution remains `msgctl`. |
| #212 public balance/ledger | Resolved via #208, privacy/permission regression and anonymous production reads. |
| #213 graceful shutdown | Sandbox cancellation now propagates after child cleanup, preserving the in-flight lease. Disposable PostgreSQL/TestRoot SIGTERM probes cover drain, exhausted stop budget and network timeout; production in-flight drain remains unverified. `scripts/check_graceful_shutdown.py --installed` runs an isolated installed-package probe without production config or recipients. |
| #214 recovery schema catalogue | Resolved: money_visibility is covered by catalogue and real recovery regression, included in production. No production promotion is claimed. |
| #215 instance paths | #222/#226 instance and hosting-settings contracts are in source. Runtime now checks the actual Root directory, named services validate all storage paths before loading the application, and preparation rejects private Root artifacts before ownership changes. Legacy rotation journals/history retain their location; ambiguous sources fail closed. [Migration runbook](ISSUE_215_INSTANCE_MIGRATION.md); production directory migration remains pending. |
| #216 same-instance domain aliases | PR #225 implements explicit ingress aliases with unchanged canonical signing authority and local identity, endpoint selection in all transports, strict hosting ingress, and retained recovery policy. The 74-test contract and installed rehearsal passed on source 210d2cf/test merge f5c45b3; later workflow-prerequisite changes and the final full gate need their own verification. DNS/TLS/proxy configuration and target rollout are separate operator steps. |
| #217 archive retention | [Read-only retention planner](DEPLOYMENT_RETENTION.md) protects current and verified rollback releases, signatures, business backups and unknown files. No deletion option; no production cleanup or rollback attestation was fabricated. Real rollback acceptance, file review and cleanup remain pending. |
| #220 retired apps | [Retirement runbook](PROJECT_RETIREMENT.md) separates `lmm-api`/`cortexfs` ownership from shared services and MSG. Actual target retirement and disk/service acceptance remain pending; no remote cleanup occurred. |
| #218 PIN record location | Resolved privately from historical records; no secret or credential path is published. |
| #219 identity selection | Offline `identity show` reports the selected authentication method without opening secret files or probing hardware. Lost-marker legacy recovery rejects ambiguous configured profiles until explicit account selection. Old server-account archival and real upgrade/recovery interaction remain separate. |
| #221 SSH-style client connections | Resolved: shipped in 0.2.3; installed-client verification, 40 connection checks and real read/post/reply/Git regression recorded on the issue. |

## Remaining requirements

| Issue | Current evidence | Work needed before closure |
| --- | --- | --- |
| [#64](https://github.com/TokenNotIncluded/msg/issues/64) ingress secrets | Actual nginx/listener inventory; loopback writer; site logs off; global main error suppression and safe inherited HTTP log format installed; actual configuration test/reload/site checks and isolated metrics/redaction regression passed | Complete all default-vhost and upstream/collector inventory and controlled whole-chain sentinel/error evidence. The installed shared policy does not certify an external collector. |
| [#65](https://github.com/TokenNotIncluded/msg/issues/65) legacy ledger | Read-only target inventory: historical typed-schema inventory and fixed-old-source evidence; current production now has money ledger entries; fixed-old-source fixture and migration rollback tests exist | Protected real old PostgreSQL ledger snapshot with provenance/freeze point; isolated migration/rollback. The server legacy backup was inspected read-only: SQLite, 33 tables, no financial tables. That SQLite backup and current money activity cannot substitute for a real old PostgreSQL ledger snapshot. |
| [#68](https://github.com/TokenNotIncluded/msg/issues/68) historical ciphertext | Existing custodial inventory/ACK/rewrap/retirement tests and root-signed backup-retirement verification | Match every original scenario to its assertion; real retained ciphertext decrypted by its authorized client; independently bounded backup-retirement facts. No key destruction is inferred. |
| [#69](https://github.com/TokenNotIncluded/msg/issues/69) revocation after restore | Proof/current-authority reconcile/promotion code exists; installed restore checks retain quarantine after marker removal | Full scenario-to-assertion review; actual independently current checkpoint outside the rollback set, protected snapshot and controlled-console promotion. Do not repeat the obsolete claim that promotion has no implementation. |
| [#70](https://github.com/TokenNotIncluded/msg/issues/70) Root/CA governance | Target inventory found Root and online CA with finite signed policies; no trust changes; latest-operation gaps identified | Exact signed grants/scopes/constraints/depth/expiry inventory and approved per-chain reissue/revoke plan; full lifecycle matrix and real VT/serial success. SSH opt-ins do not prove physical-console success. |
| [#76](https://github.com/TokenNotIncluded/msg/issues/76) authorization | Source/adapter, cache, attachment, concurrent revocation and quarantine regression families pass in the complete run | Original source × representation × invalidation requirements need explicit assertion mapping, including independent legal-source positives and field/count/digest privacy. |
| [#77](https://github.com/TokenNotIncluded/msg/issues/77) personal/Legacy/honors | Existing signed personal content, Legacy and R1–R5 test families in full run | Map every history/key/invalidation/challenge/CLI/TUI requirement to concrete assertions; separate protocol completion from identity/trust claims. |
| [#78](https://github.com/TokenNotIncluded/msg/issues/78) read/search/Sync | `READ_ENTRY_ACCEPTANCE.md` maps supported semantics; PR #206 restores immutable v1 dictionary/schema and version binding | Complete original budget/pagination/filter/output/current-authority mapping. Representative dispatcher equivalence does not by itself prove every predicate boundary. |
| [#79](https://github.com/TokenNotIncluded/msg/issues/79) content/Revision | File, patch/rebase/batch, signed Revision and physical-failure tests in complete run | Map all original mutation/metadata/scope/owner/group/generation/template/link/limit requirements; exact filesystem orphan behavior must remain distinct from DB atomicity. |
| [#80](https://github.com/TokenNotIncluded/msg/issues/80) Transfer/Git/LFS/hosting | Real installed OpenSSH/Git and backup/recovery checks; Transfer/LFS/hosting regressions | Full clause matrix plus target shared-volume/multi-instance budget, disk-limit, power-loss/GC/restore durability. Single-process locks are not multi-instance evidence. |
| [#81](https://github.com/TokenNotIncluded/msg/issues/81) events/notifications/collaboration | Current-attempt/deadline fences and controlled isolated sender tests | Event/state/privacy/current-permission assertion matrix; controlled target SMTP/Webhook success/failure/uncertain/retry/recovery evidence. No arbitrary production recipients. |
| [#82](https://github.com/TokenNotIncluded/msg/issues/82) clients/tools/SSH/RSS | RSS issue resolved; XDG credentials/native layout, environment-based TUI locales, installed-client/SSH and sandbox regressions | Remaining view/output/retry/explicit-write clause map; target application SSH/PAM and bwrap resource/network-limit matrix. Administrator SSH port 22 is not the application's SSH endpoint. |
| [#83](https://github.com/TokenNotIncluded/msg/issues/83) full design acceptance | Immutable published read contract repaired; exact 2,851+8 evidence gate; finite design inventory exists | All 564 design obligations need concrete positive/negative/concurrency/recovery assertions where applicable. Inventory navigation entries alone are not assertion coverage. Refresh outdated diagnostic-gap entries against actual main. |
| [#84](https://github.com/TokenNotIncluded/msg/issues/84) production/recovery | Native 0.2.14 baseline verified as described above; protected backup/rollback material retained. Historical selftests and complete CI runs retain their original source/scope and cannot certify later changes | Legacy snapshot consistency and independently current authority; actual target capacity/resource exhaustion/shared-topology recovery, cutover and stopping criteria; enabling operations beyond the installed signed ceiling still requires the corresponding CA policy transition. |
| [#85](https://github.com/TokenNotIncluded/msg/issues/85) architecture/test debt | Complete current CI, shared protected ledger/market owners and effect fences | Measure current duplication/hot paths and trace all convergence invariants; keep published versions/signatures and history. Green Ruff or smaller files are not architectural acceptance. |

## Concrete fixes in the 2026-10-02 iteration

The Transfer authorization hook no longer dereferences a missing HTTP request
during internal upload authorization. Frozen board rules also apply to the open
policy path, closing move/restore mutations while frozen. Empty and EOF FUSE
reads recheck current file permissions instead of bypassing revocation.
Homepage Markdown branding escapes user text before insertion into table rows.

Shared-board draft/conflict/retry handling, live polling cancellation and stale
status, responsive navigation, translated labels and terminal cancellation/IME
behavior have focused Python and real JavaScript regression checks. Public-board
controls now remain in a discreet menu and the default artwork is outline ASCII;
existing user revisions are preserved. Multi-page agent styling and compact
Markdown/JSON reads at `/now` and `/terminal` also have explicit negotiation,
HEAD, rejected media type and anonymous-access tests. Final-source CI and the
installed native package remain independent evidence.

New UI and operations do not grant new signed capabilities. The installed Root
and online CA policy still rejects newer board/drop/rules operations outside its
issuance ceiling; logging in or renewing an ordinary credential cannot enlarge
that ceiling. No Root rotation or direct database grant was performed.

The issues above remain open wherever their original field acceptance is still
missing. A partial source repair or read-only runbook does not close an epic.

## Historical repairs and evidence

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

The shared Nginx ingress policy is now installed on `archczy`. The root/HTTP
error policy suppresses request-bearing error text and the shared access format
retains only timestamp, configured server, status and byte count. The original
configuration is backed up at
`/var/backups/msgd/deployments/ingress-policy-20261001/nginx.conf`; previous logs
were retained. Actual `nginx -t` and reload passed, followed by HTTP 200 from the
health endpoint, Markdown homepage and shared status site. All six ingress tests
passed locally, including a real isolated shared listener whose normal/400/414
status records contain no URL/header sentinel. External collectors remain unverified.

The complete target CA report is stored privately at
`~/.local/state/msg/admin/archczy/preflight-20261001.json` (0600). It contains both
CA signed-policy snapshots, including scopes/constraints/validity/depth, and records
`mutation_performed=false` and `decision=blocked`. No CA key was unlocked.

The capability builder also had a version-dependent exclusion defect: only v1
of privileged purge/chown operations was excluded from ordinary base grants.
A new assembly regression first failed with v2 in the base set, then passed after
excluding every registered version by name. Dedicated privileged capabilities
still retain both versions, and ordinary content operations remain in base grants.
All 32 capability/old-authority/online-CA/delegation-depth regressions passed.
This fixes the future-version ceiling defect; it does not claim an exploit in
currently deployed v1 handlers or change existing signed certificates.

PR #205 passed every current-head check and was merged at `36119c5`. It restores
anonymous GET/HEAD crawler discovery, listing only the unconditional homepage.
The targeted crawler/branding/dictionary selection passed all 32 tests. Its full
run is [36756860534](https://github.com/TokenNotIncluded/msg/actions/runs/36756860534);
independent merged-main acceptance is a separate check.

## Verified source evidence

[PR #206](https://github.com/TokenNotIncluded/msg/pull/206) head
`ab301ca4b770f5917f2c57c2a09aa2313ef543b9`, tree
`e7e75a0a38fcf2d624c5f67bdf64a1e70c2184ca`, matches merged main `5688012`.
[Complete run 36753933957](https://github.com/TokenNotIncluded/msg/actions/runs/36753933957)
contains eight digest-verified shard artifacts; `scripts/ci_shards.py check 8`
independently confirms the exact 2,851-node disjoint union. Conformance has
8 tests and no failure/error/skipped elements. Current-head installed rehearsal
and specialized checks also passed. Later patches need separate validation.

For #72, `BOUNTY_ACCEPTANCE.md` maps the original clauses to concrete assertions.
Its 30 explicit named nodes are all in that exact run, with the whole-file
contracts and installed official market fixture. The issue's isolated funding
and notification scope is fulfilled; separate field gates stay in #70/#81/#84.

## Operator materials still needed

Real-snapshot/console/retirement acceptance requires protected materials with source timestamps and independently current evidence. Routine deployment decisions are delegated to the operator; unavailable evidence must remain explicitly unresolved rather than substituted with an SSH or fixture run.

A blanket Root rotation would invalidate existing chains. The compatible upgrade separates anonymous-only homepage v4 from credential permissions and explicitly disables renaming where the installed CA has not authorized it. It preserves published v1 meanings and existing signed authority; enabling the rename feature still requires the appropriate signed policy. Current source, deployed source and signed authority are reported separately. None of these issues authorizes inventing attestation evidence.
