# Isolated OpenSSH and Git acceptance

This is local, disposable acceptance evidence for #80/#82, not a target-host
installation or a production acceptance claim. Source baseline: `2eb1ea3`.
Run explicitly as an ordinary user with noninteractive sudo:

```sh
uv run python scripts/check_isolated_sshd.py
```

The script creates a fresh PostgreSQL cluster/database, application configuration,
service data, independent Root/identity/SSH/host keys, localhost port and daemon.
It protects its own Root directory from the service user. Its AuthorizedKeysCommand
wrapper lives in a unique root-owned `/run/msg-sshd-check-*` directory: OpenSSH
correctly rejects an otherwise root-owned wrapper below world-writable `/tmp`.
StrictModes is never disabled. Existing sshd configuration, administrator keys,
system services and production databases are not used or changed. Cleanup stops
the specific daemon and multiplexed client, removes the temporary privileged
wrapper and Root directory, and destroys the disposable installation/database.
The script is deliberately separate from ordinary pytest: it requires sudo and
real OpenSSH and fails if they are unavailable.

## Real local checks

- Registered SSH possession proof and capability ceiling; dynamic
  AuthorizedKeysCommand lookup and credential-bound forced command succeed.
- Shell commands and a Git filesystem escape are refused.
- Remote TCP forwarding is refused by the real daemon.
- The standard Git client performs atomic two-ref SSH push, ls-remote and clone;
  cloned file bytes match the source.
- A revoked certificate is refused both by a new login and by a new channel
  inside an already authenticated ControlMaster connection. The optional
  certificate is explicitly detached afterward; it is never unrevoked.
- SSH key revocation is rechecked inside the existing connection, and subsequent
  fresh SSH/Git connections fail authentication.

The focused existing test
`tests/test_ssh_rss.py::test_git_reference_guard_real_commit_and_revoked_key`
also now sends a real `git update-ref --stdin` transaction containing two creates
and `prepare`, then ends stdin before `commit`. Both refs are absent afterward,
the original ref remains, and repository metadata generation is unchanged.
This is a prepared-transaction EOF abort, not a power-cut simulation.

## Existing coverage inspected, without repeating broad suites

- `test_git_http_push.py`: real HTTP Git client, retry/idempotency conflicts,
  interrupted pack staging cleanup, size boundary and revoked-token ref hook.
- `test_git_lfs.py::test_real_git_lfs_push_and_pull`: actual git-lfs client.
- `issue_78_80/test_lfs_integrity.py`: oid/content corruption, crash before legacy
  replacement, retained historical ACL root, real-process capacity locking,
  symlink rejection and GC retention.
- `test_lfs_shared_gc_quota.py`: shared CAS inode, ACL root and deployment-wide
  capacity/volume identity.
- `test_effect_completion_fencing.py`: expired attempt/deadline prevents physical
  Git publication; stale completion cannot publish successful metadata.
- `test_ssh_process_identity.py`: unprivileged process-name spoofing is refused.

These existing tests do not turn the new local script into full #80/#82 closure.

## Still needs target-host or additional fault evidence

Archczy must validate its actual service account, packaged interpreter, deployed
AuthorizedKeysCommand/forced command paths, SSH/PAM/systemd configuration,
filesystem permissions, firewall/proxy/log policy and service restart behavior.
This script's local success does not certify those deployment facts. Remaining
subject/group/ceiling and full client UI matrices must retain their own evidence.

A true multi-storage durability claim needs controlled crash/restart or power-cut
experiments across PostgreSQL, Git refs, CAS/LFS staging and GC on the intended
filesystem/shared-volume topology. The EOF test above cannot establish it.
Cross-instance reservation/cleanup and host failover must likewise be tested with
the actual shared storage and worker topology. These are distinct from the
ordinary-client/sshd checks that can already run locally.

## Observed result on 2026-09-29

The explicit script completed all seven checks and reported
`temporary_installation_removed: true`. Follow-up inspection found no remaining
`/run/msg-sshd-check-*` wrapper, temporary installation or isolated daemon.
The focused prepared multi-ref EOF test passed (`1 passed in 11.02s`).
Environment: Linux 7.2.7-arch1-1, Python 3.15.0rc2, OpenSSH 10.5p1,
Git 2.55.0 and git-lfs 3.8.0. No target-host deployment was performed.
