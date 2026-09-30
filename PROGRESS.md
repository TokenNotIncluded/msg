# Delivery progress · 2026-10-01

The goal is to resolve every open issue against its original requirements.
Implementation, isolated acceptance, target-host evidence and deployment are
separate facts. [The current issue ledger](docs/ISSUE_RESOLUTION.md) records the
remaining work; historical runs retain their original source provenance.

## Current source and verification

- Main includes PR #206 at `5688012eaa4b588950aba5087de16dfe035dcd71`.
  Its tree matches the tested PR head `ab301ca4b770f5917f2c57c2a09aa2313ef543b9`.
- [Rewrite contracts 36753933957](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36753933957):
  all eight artifact ZIP SHA-256 digests checked; the local exact-node gate
  confirms 2,851 core tests, a complete disjoint union, no failures/errors/skips.
  The same artifact set includes 8 passing conformance tests and wheel/sdist.
- All current-head PR #206 checks passed, including installed-server rehearsal,
  client, recovery, hosting, architecture and both integration locales.
  Review bots exhausted their quotas; no external review approval is claimed.
- PR #206 restores the published `discovery.read_query@1` schema, introduces
  anonymous-only homepage summaries at v4, preserves decoded short-code versions,
  and checks the configured restore marker before removing it in the rehearsal.
- PR #205 is being checked against the corrected main. Its old failing checks
  are not acceptance of the newly combined source.

## Resolved issues

- #202: both deployed RSS endpoints reject `limit=²` with HTTP 400
  `invalid_limit`; the RSS and homepage regression selection passed locally.
- #200: the deployed Markdown homepage has current public counts, five recent
  canonical links/timestamps, Asia/Taipei dates and platform/topic rule links.
  Existing permission tests cover private, archived and untraversable resources.
- #72: the original isolated bounty/official-market acceptance is covered by
  `docs/BOUNTY_ACCEPTANCE.md`. All 30 explicitly named test nodes occur in the
  verified complete run, together with its whole-file contract tests. Installed
  official selftest passed all 42 checks on the deployed release. This does not
  claim physical-console, production-funding or external-mail acceptance.

## Target host

`archczy` currently runs native package `msgctl-server 0.2.0-20261001.1`, source
`46b7cbf7a157012a9dea62a2bf3a9e330079cfd6`. Main is newer than deployment.
Package integrity, health, doctor and isolated official selftest were verified
for that installed release. Root trust and existing account keys were preserved.

The September 30 read-only preflight found PostgreSQL 18, 87 tables, a typed
ledger with zero ledger entries and zero legacy escrow identities, and two finite
CA certificates. This current database is not a legacy production snapshot.

New `identity.rename@1` and `discovery.read_query@4` authority is absent from the
installed signed CA snapshots. Existing certificates must not acquire it merely
because code was updated. Root rotation invalidates old chains; an upgrade must
first have a concrete, approved CA transition rather than silently changing trust.

The shared Nginx global/pre-Host policy is now installed: inherited access logs
keep only time/configured server/status/bytes and error text is suppressed.
Configuration test/reload and the homepage, health and shared status-site requests
passed; old logs were retained. Upstream/collector evidence is still incomplete,
so #64 remains open. See the issue ledger for all other unresolved conditions.

## Completion gate

New changes require their own verification. The PR #206 results do not certify
later changes. [Release acceptance](docs/RELEASE_ACCEPTANCE.md) defines the full
suite, exact node-ID gate, installed package checks and applicable specialized
checks. A legacy snapshot, independent current checkpoint, real retained-ciphertext
and backup-retirement evidence, physical/serial console, controlled notification
recipients and target-topology durability measurements remain distinct requirements.

The previous progress snapshot is retained in Git history at `5dbc03b:PROGRESS.md`;
its historical test counts must not be added to the current run.
