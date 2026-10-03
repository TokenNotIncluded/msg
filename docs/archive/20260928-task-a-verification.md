# Historical: Task A verification — 2026-09-28

Historical source tree: `ff7c49364227b9d2a0e4fabdcdbe4208c811c094`.

This frozen record preserves the independently identified local PostgreSQL/age
test evidence below. Its source, counts and pending gates describe that earlier
candidate; they do not describe the current checkout or authorize recovery.
Current operator instructions are in [recovery](../TASK_A_RECOVERY_20260928.md),
[complete recovery proof](../COMPLETE_RECOVERY_PROOF.md) and
[release acceptance](../RELEASE_ACCEPTANCE.md).

Refs #64, #65, #68, #69, #70, #84, #85. Incremental PR #116 is stacked on #112. C remains the sole serial merger. This record is not production approval.

## Exact source provenance

- Starting main: `67401759a853c69fc5cb234982e1f7020b650542`.
- Combined #112 base: `512bb9b1e163151d66b94a5bc26f4b3cc6415070`.
- Local ten-commit head: `4f3a5bd760559bebd4ef2dac2bfa153bdecf7f67`.
- Published ten-commit head: `88c1da3e7d3a2055682c9cd3d9083f52ff5159a6`.
- Both code trees: `ff7c49364227b9d2a0e4fabdcdbe4208c811c094`.
- Mailbox SHA-256: `645fa0d00159db087a5b69e088ace801b8c8194425142b3dec162f09cc997054`.

Publication run `36400846976` checked mailbox bytes, the exact base, ten commits and exact final tree before a normal fast-forward push to A's exclusive functional branch. Author dates/test-first commit order were preserved; committer metadata changed, hence different commit IDs. No original #112/B/C/main ref was updated. Temporary assets/publication workflows and transport files exist only on the separate auxiliary branch, not in PR #116.

## Actual local combined run

Environment: disposable Debian 13, Python 3.13.5, PostgreSQL 17.11, age 1.2.1, real Git; the pinned legacy source is `ac083b3666b1b5268d90115129e2cc4e0dd11521`. Local runtime versions are deliberately distinguished from the required target CI versions.

```sh
python -m pytest tests/test_task_a_*.py \
  tests/test_custodial_upgrade.py tests/test_custody_exit_evidence.py \
  tests/test_custodial_lifecycle.py tests/test_root_rotation.py \
  tests/test_root_rotation_resume.py tests/test_root_storage_layout.py \
  tests/test_authorization_recovery_boundaries.py \
  -q --tb=short --junitxml=a-suite.xml
```

Result: **58 passed in 188.28 seconds; zero failure, error or skipped test**. Includes actual PostgreSQL backend termination before commit, concurrent/repeated replay, real age/Ed25519 custody recovery, Root file-permission/hardlink/symlink rejection, preparation races, and actual backup/restore with separately asserted current authorization sources and external quarantine.

- `a-suite.xml` SHA-256: `413a3d4e715535f0b0d1e80f2c2ee9629c67f066ecfbe5a027574b8f5e3ad2b5`.
- `a-suite.log` SHA-256: `4b0028b7dcd6bd364874cd584408a149c25665a60dabd2fe8402ff435a7a3bf7`.
- Empty-history binding pre-fix log, 9 fail / 1 pass: `1a02359ab9091f2e3a3461a0adc516ea1225abdefb0238e54c0fd21a238fd083`.
- Root pre-fix behavioral regression log, 5 fail: `bdfc7f72ced936b85222741d3ba7e35a141ee4858b2b98418b79c4177c003eb6`.
- Quarantine/authorization pre-fix log, 1 fail: `9f9f095dcaf169e40cd06b7b79f6ecbe8ce39f0d4af521aac45b1b93fc7dff59`.

Compileall, diff whitespace checks, and Ruff on the new standalone modules/tests passed. Earlier focused run counts overlap this combined run and must not be summed as additional tests.

## Required final-head gate

The unchanged repository CI must run after this documentation commit: Python 3.15, PostgreSQL 16, Valkey 9, real age/Git, all four core shards, exact JUnit node-ID disjointness/completeness gate, all conformance and wheel/sdist. Collection of the local code tree found 1,399 core tests; the final actual collected manifest remains authoritative, not a hard-coded historical number.

At this commit the final-head CI result is **not yet verified**. Append actual run IDs, source SHA and artifact checks to the PR/issue discussion without changing this record merely to manufacture another head. Neither publication success nor the local 58-case run substitutes for that gate.

## Scope limits and unavailable evidence

The replay module applies a finite deny-only fact contract and **never promotes or clears quarantine**. Complete current ACL/TopicBan/policy reconciliation, independently current checkpoint provisioning and controlled production promotion remain unfinished acceptance work. A database watermark is only transactional progress, never the independent anti-rollback trust source. Offline/backup key retirement is not inferred from online key deletion.

No production configuration/log-chain access, protected production snapshot, real console/CA transition approval, capacity run or actual outbound delivery evidence was supplied. The read-only preflight therefore reports field acceptance blocked, including on a consistent test database. No production deployment, Root/PIN/key operation, fund movement, restore/delete or real mail/webhook was performed.

See `docs/TASK_A_RECOVERY_20260928.md` for module interfaces, operator commands, B's source-only bank/migration cross-review and exact remaining inputs for C.
