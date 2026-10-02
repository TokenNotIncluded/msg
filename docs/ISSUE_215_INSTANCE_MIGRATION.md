# Instance migration status and acceptance

Issue [#215](https://github.com/TokenNotIncluded/msg/issues/215) remains open until the existing installation has moved to an isolated named layout and passed the checks below. PR #222 implements stable instance paths and service templates; PR #226 fixes its hosting configuration registration. Installing that code preserves the old layout and does not move an existing Root, database or identity.

## Production snapshot: 2026-10-02

Read-only package, systemd and directory metadata checks on the `msg.lmm.best` server established:

| Check | Observed state |
| --- | --- |
| Native package | `msgd 0.2.14-20261002.33` |
| Active writer | `msgd.service`, user/group `msgd`, working directory `/var/lib/msgd` |
| Active worker | `msgd-worker.service`, user/group `msgd`, working directory `/var/lib/msgd` |
| Named templates | `msgd@.service` and `msgd-worker@.service` installed, disabled |
| Configuration parent | `/etc/msgd`, `root:root`, `0755` |
| Legacy data parent | `/var/lib/msgd`, `msgd:msgd`, `0750` |
| Legacy Root directory | `/var/lib/msgd-root`, `root:root`, `0700` |
| Named private parent | `/var/lib/private/msgd`, `root:root`, `0700` |

This establishes template installation and continued singleton operation. The existence of a private parent directory does not establish that Root material has moved. This audit did not read private keys, configuration contents or database credentials, and performed no production migration or Root initialization. The package version is a dated observation, not a claim about later releases.

The source audit also reproduced and fixed three startup/preparation gaps: the network permission gate now checks the actual private Root directory as well as the legacy configuration child; named runtime rejects storage paths belonging to a different layout; and named configuration/preparation refuses known private Root artifacts before ownership changes. Read or search access to a protected Root directory is sufficient to reject network startup. These source changes do not establish that a later package has been installed or that production has migrated.

Repeat the metadata checks without printing configuration or secret files:

```sh
pacman -Q msgd
systemctl show msgd.service msgd-worker.service --property=Id,ActiveState,SubState,User,Group,WorkingDirectory,FragmentPath
systemctl list-unit-files msgd@.service msgd-worker@.service --no-pager
sudo stat -c '%n %U:%G %a' /etc/msgd /var/lib/msgd /var/lib/msgd-root /var/lib/private/msgd
```

## Preserve the existing logical instance

Moving host directories keeps the same signed service origin, Root trust, subjects, credentials and ledger. It is distinct from the [new Root legacy import](NEW_ROOT_LEGACY_IMPORT_RUNBOOK.md), which intentionally establishes new authority. `msgd --instance NAME init` creates a new installation; it is not a migration command for an existing Root. `prepare-instance.sh` prepares permissions for a completed named installation and refuses mixed legacy/named layouts; it does not copy data, resolve conflicts or provide a migration journal.

Before moving anything, prepare an offline plan and protected evidence directory. Record source and destination paths, the current package and service units, canonical `service_url`, public Root fingerprint, database identity, service account and proxy target. Store complete configuration, dumps and key material only in protected backups; public acceptance evidence needs their digests and outcomes, not contents. Use the existing physical-console Root backup procedure separately from the service-data backup, which excludes the Root private envelope.

The destination uses a stable name such as `main`, independent of DNS:

| Purpose | Destination |
| --- | --- |
| Configuration and trust | `/etc/msgd/main` |
| Persistent data and online service keys | `/var/lib/msgd/main` |
| Existing Root private state | `/var/lib/private/msgd/main/root` |
| Rebuildable cache / runtime | `/var/cache/msgd/main` / `/run/msgd/main` |
| Writer / worker user | `msgd-main` |
| Writer / worker units | `msgd@main.service` / `msgd-worker@main.service` |

For this path relocation, keep the existing database identity. A second independent installation needs its own database, database role, service account, keys and ports; a different directory or a different libpq service name alone does not establish database isolation.

## Offline migration acceptance

1. Stop the existing writer, worker and any other writers to its persistent state. Record the freeze point and protect a restorable PostgreSQL/Git/CAS/staging backup and the existing Root envelope. Rehearse recovery in an isolated target with outbound effects disabled before scheduling production changes.
2. Check every source, destination and parent for symlinks, ownership and conflicting contents. Check both supported configuration names and all supported Root locations. Conflicting `msgd.toml`/`server.toml` or legacy/named Root material must be resolved explicitly, preserving both protected copies. Existing destination directories are a conflict; do not merge their identities or overwrite keys.
3. Follow a journaled copy/move plan. The named configuration and data paths are children of the old shared parents, so a whole-directory move into its own child is not a valid plan. Preserve a protected original snapshot outside those parents. Keep content, repositories, CAS, resumable staging, online keys, trust, journals and private Root state; copy the existing Root envelope rather than invoking initialization or rotation.
4. Update only host path references. Preserve the exact signed `service_url`, database identity, trust anchors and credentials. Review explicit libpq, mail, hosting, recovery-marker and other file references as well as `[storage]`. The named units select their own `PGSERVICEFILE`/`PGPASSFILE`; their protected files and database permissions must match the selected instance. Move the existing recovery marker with persistent data and keep the hosting reader pointed at it.
5. Set shared configuration/data/cache/runtime parents to OS-root ownership. Limit the `msgd-main` account to its own data, cache and runtime children. Keep every Root ancestor OS-root-owned, Root directories `0700`, private files `0600`, and online service keys readable only under their intended service policy. A Root directory under a service-writable parent does not provide isolation even when the key itself is `0600`. Do not run legacy recursive preparation over a named installation.
6. Validate the completed named layout before starting its units. Record `msgd --instance main doctor` results, Root/public trust and online-key fingerprints, subject and credential counts, ledger totals, committed event/state heads, content references and pending durable effects. Compare against the frozen source; an authenticated read with an existing credential must still succeed. Keep complete outputs private when they contain account or configuration metadata.
7. Start only the named writer on a nonconflicting loopback port. Confirm its real unit user, working directory and runtime paths. Check that the service account cannot read the Root hierarchy or another instance's protected configuration, keys or data. Keep legacy units stopped. Independently accept worker isolation and effects handling before starting the named worker; queued external effects must not be replayed during a rehearsal.
8. Verify public and authenticated routes, content/transfer reads, existing identities and ledger history. Record named-unit startup and a controlled worker cycle against the same preserved database. Only then switch the proxy and verify the real public origin. Preserve the old snapshots and migration journal until this acceptance and rollback rehearsal are complete.

These steps are an operator acceptance plan, not evidence that they have happened. The source tests cover path selection, invalid names, explicit instance inputs, Root lookup compatibility/conflicts and service-template declarations. They do not establish a production directory move, independent database roles or filesystem access boundaries under the actual service account.

## Rollback and closure evidence

Before new writes, rollback can stop the named units, restore the protected original paths and configuration, and return to the original units/proxy with the same Root and database. Keep the named writer and worker stopped while doing so. After new writes, freeze the named processes and preserve their current database, content and staging state first; an old snapshot cannot simply replace that state. Reconcile changes or remain in maintenance until a verified recovery plan preserves them.

Close #215 only with evidence for the actual named units and paths, unchanged Root and logical database identity, preserved subjects/credentials/ledger/content, service-account denial of Root access, authenticated behavior, accepted worker processing and a successful rollback rehearsal. Record the deployed package/commit and backup/state digests. Keep Root material and credentials out of issue comments. Template installation or a passing source test alone leaves the migration acceptance open.
