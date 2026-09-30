# Filesystem layout

Host filesystem paths follow the [XDG Base Directory specification](https://specifications.freedesktop.org/basedir/latest/) for the client and [FHS](https://refspecs.linuxfoundation.org/FHS_3.0/fhs/index.html) for the Linux service. MSG resource paths such as `/main` and `/@root/web`, URLs, sandbox mount points, and Git tree paths belong to separate protocols; they are not rewritten as host paths.

## Client

`src/msg/paths.py` owns layout selection and private directory validation. Empty or relative XDG base variables use the standard defaults. Normal client paths append `msg/services/<canonical-domain>` to each base directory. One service origin has one identity; `--profile NAME` stores only a service-selection alias in configuration. Alias names allow letters, digits, underscores and hyphens, up to 64 characters. Choose an initial destination, `--server`, or `MSG_SERVER`; there is no hardcoded public service. See [connection configuration](CLIENT_CONNECTIONS.md) for SSH-style destinations and `Host` entries.

| Purpose | Default directory | Contents |
| --- | --- | --- |
| Configuration | `~/.config/msg` | SSH-style `config`, service-selection aliases and per-service configuration; never new private keys or tokens |
| Durable identity data | `~/.local/share/msg/services/<domain>` | Signing keys, encryption keys including rotated keys, CA keys, exported certificates, Bank approval records, retained custodial recovery history |
| Persistent state | `~/.local/state/msg/services/<domain>` | `client.json` (server binding, tokens and certificate IDs), registration and operation journals, OAuth sessions, upgrade locks |
| Disposable cache | `~/.cache/msg/services/<domain>` | Verified operation catalogs |
| Temporary work | `$XDG_RUNTIME_DIR/msg`, or Python's temporary directory | Unique private working directories, removed when the operation exits; Python honors `TMPDIR`, `TEMP`, and `TMP` |

Application directories are owned by the invoking user and mode `0700`; credentials and session files are `0600`. Symlinked application directories are rejected before changing permissions. Generic XDG base directories are never chmodded by the client. A configured runtime directory must already be owned by the user and private. Do not put a profile in a shared directory.

Stop clients using the old profile before migrating:

```sh
msg --server https://msg.lmm.best --profile lightjunction --migrate-from ~/.config/msg/profiles/archczy/lightjunction identity show
msg --profile lightjunction tui
```

Migration checks every source file and every destination conflict before writing. It preserves keys and resumable journals, writes copies durably, verifies them, and only then removes original files. Identical copies from an interrupted migration are accepted; different credentials are never overwritten. Legacy catalog caches and nested profile containers remain in place and can be removed separately after migration. Legacy single-directory and split XDG profiles migrate to their recorded service on first use. A migration explicitly targeting a different service stops before creating destination files. Split-profile keys and state are copied and checked together; distinct identities for the same domain are never merged or overwritten. Explicit `--config-dir DIRECTORY` retains the old portable single-directory layout for existing integrations; it cannot be combined with `--profile` or `--migrate-from`.

## Service

| Purpose | Standard path |
| --- | --- |
| Commands | `/usr/bin/msg`, `/usr/bin/msgd` |
| Package-owned runtime and application | `/usr/lib/msgd` |
| Configuration and public trust | `/etc/msgd`, `/etc/msgd/trust` |
| Persistent content | `/var/lib/msgd/git/content` |
| Public repositories | `/var/lib/msgd/git/repos` |
| Content-addressed blobs | `/var/lib/msgd/blobs/sha256` |
| Resumable transfer staging | `/var/lib/msgd/transfers/staging` |
| Online service keys | `/var/lib/msgd/service` |
| Root CA private state | `/var/lib/msgd-root` |
| Disposable service cache | `/var/cache/msgd` |
| Runtime files | `/run/msgd` |
| Protected deployment backups | `/var/backups/msgd` |

Arch, Debian, and RPM packages share this layout. Each installation requires an explicit service origin and has its own Root trust, CA policies and stored identities; the package name and source repository do not bind it to a public domain. Separate installations must use separate configuration, data and protected Root directories. `msgd init` requires `--service-url`; the source helper's example origin is loopback only. Package files belong to the package manager; service data and Root CA private state survive upgrades. The network service cannot access `/var/lib/msgd-root`. Root initialization intent now lives beside the protected root key, rather than under `/etc`. Recovery quarantine markers now live in the persistent data directory; old configuration-directory markers remain recognized. Both markers at once fail closed. The hosting reader's explicit recovery-marker configuration must point to the writer's marker.

Explicit server storage paths must be absolute and cannot contain `..`. Custom configuration directories and isolated test installations remain supported. Missing staging paths derive from that installation's data location instead of silently using production storage. A custom configuration directory uses a sibling `<config-name>-root` directory for Root CA state. Existing `server.toml`, legacy embedded blob/repository layouts, and `/etc/msgd/root/key.json` remain compatibility inputs; migration must preserve their security boundaries.

## Audit coverage

The audit covers client state and all client helpers, service configuration and administrative defaults, root initialization, backup/restore and recovery promotion, worker and Git staging, atomic writes, sandbox mounts, native packaging, deployment templates, and verification scripts. `scripts/audit_filesystem_paths.py` lists the tracked filesystem call sites and deployment path declarations for repeatable review.

Atomic replacement temporaries intentionally remain in the destination directory so rename is atomic even across mounts. Resumable uploads remain persistent; they are not cache. Worker/Git scratch directories use their configured staging roots. Disposable self-tests honor the temporary-directory environment. `/proc`, `/dev/null`, system executable paths, TLS trust paths, and sandbox-internal `/output` are operating-system interfaces, not application storage defaults.

## Account removal

`msgd account archive SUBJECT_ID` requires the Root ceremony; `--allow-ssh` explicitly permits an OS-root SSH terminal for this command only. It presents an exact preview digest and requires the Root passphrase. It archives the user profile, revokes credentials, advances the authentication version, and records a Root-signed audit event. Authentication and SSH reject archived identities. Account, resource, and ledger history remain intact. Root/system identities, active Bank roles, and nonzero balances must be resolved before archival.

## Named service instances

New installations select a stable instance identifier, **not a domain name**:

```sh
sudo msgd --instance main init --service-url https://example.org --postgres-dsn service=msgd-main --allow-ssh
sudo deploy/prepare-instance.sh main
sudo systemctl enable --now msgd@main msgd-worker@main
```

Names match `[a-z][a-z0-9_-]{0,25}`. `--instance` and `--config-dir` are mutually exclusive. Named initialization requires an explicit public origin and an explicit database; separate instances need separate databases, service keys, trust chains, accounts and listening ports. Set each instance's `listen`/`port` before enabling it. The example hostname is configuration, never an installation identifier.

| Purpose | Named-instance path (`main`) |
| --- | --- |
| Configuration/public trust | `/etc/msgd/main`, `/etc/msgd/main/trust` |
| Persistent content, blobs, service keys | `/var/lib/msgd/main` |
| Root private state | `/var/lib/private/msgd/main/root` |
| Cache | `/var/cache/msgd/main` |
| Runtime | `/run/msgd/main` |
| Network/worker account | `msgd-main` |
| Systemd units | `msgd@main.service`, `msgd-worker@main.service` |

Shared parent directories belong to OS root. The service owns only its instance's data directory. Root private state is outside every network-service-writable parent; the private hierarchy remains OS-root-owned and the units make it inaccessible. Never put Root keys below `/var/lib/msgd/main/root`: ownership of the parent would let the service replace that directory. `prepare-instance.sh` rejects mixed legacy/named installations, symlinks and storage-layout mismatches before changing permissions. The read-only hosting process still uses an explicit configuration directory; its credentials and paths must be isolated separately.

Existing bare `/etc/msgd`, `/var/lib/msgd` and `/var/lib/msgd-root` installations remain readable and are **not moved automatically on upgrade**. Existing explicit custom configuration directories also retain their previous Root location. This compatibility does not make the old singleton layout the recommended layout for new installations.

### Migration boundaries

Migration is an offline administration operation, not initialization. Stop both the old web process and worker, take a restorable backup and preserve a protected copy of the existing Root envelope. Reject existing destination directories and symlinked source paths. Preserve database identity, Root/public trust, online keys, token secrets, ledger data and the exact signed `service_url`; never generate replacements. Move configuration and persistent content into the selected instance, rewrite only their host storage paths, and relocate the existing Root envelope into the OS-root-owned private hierarchy. Check ownership and service inability to read Root state before switching units. Keep a journal and retain the original directories until startup, authenticated requests, worker processing and ledger checks pass. If anything fails, stop the new units and restore the original paths and configuration. `prepare-instance.sh` does not perform this migration or silently reinterpret an existing instance.

### Domains and signed identities

A single logical instance with several DNS aliases is different from several independent instances. Filesystem instance names do not encode DNS, so adding a DNS name never requires changing these paths. The current signing protocol still binds certificates and requests to the configured canonical `service_url` (`target_service`); this change deliberately preserves that binding. Until an explicit trusted-origin/alias protocol is implemented, additional names must redirect clients to the canonical origin. Do not accept arbitrary Host headers, rewrite a signed service binding, or claim that a reverse-proxy alias creates a second independent MSG instance. Changing a canonical origin requires a separate identity/credential migration; directory moves alone do not change signatures.
