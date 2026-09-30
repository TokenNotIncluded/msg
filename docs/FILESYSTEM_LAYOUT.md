# Filesystem layout

Host filesystem paths follow the [XDG Base Directory specification](https://specifications.freedesktop.org/basedir/latest/) for the client and [FHS](https://refspecs.linuxfoundation.org/FHS_3.0/fhs/index.html) for the Linux service. MSG resource paths such as `/main` and `/@root/web`, URLs, sandbox mount points, and Git tree paths belong to separate protocols; they are not rewritten as host paths.

## Client

`src/msg/paths.py` owns layout selection and private directory validation. Empty or relative XDG base variables use the standard defaults. `msg --profile NAME` appends `msg/profiles/NAME` to each base directory. Names allow letters, digits, underscores, and hyphens, up to 64 characters.

| Purpose | Default directory | Contents |
| --- | --- | --- |
| Configuration | `~/.config/msg` | Reserved for configuration; never new private keys or tokens |
| Durable identity data | `~/.local/share/msg` | Signing keys, encryption keys including rotated keys, CA keys, exported certificates, Bank approval records, retained custodial recovery history |
| Persistent state | `~/.local/state/msg` | `client.json` (server binding, tokens and certificate IDs), registration and operation journals, OAuth sessions, upgrade locks |
| Disposable cache | `~/.cache/msg` | Verified operation catalogs |
| Temporary work | `$XDG_RUNTIME_DIR/msg`, or Python's temporary directory | Unique private working directories, removed when the operation exits; Python honors `TMPDIR`, `TEMP`, and `TMP` |

Application directories are owned by the invoking user and mode `0700`; credentials and session files are `0600`. Symlinked application directories are rejected before changing permissions. Generic XDG base directories are never chmodded by the client. A configured runtime directory must already be owned by the user and private. Do not put a profile in a shared directory.

Stop clients using the old profile before migrating:

```sh
msg --profile lightjunction --migrate-from ~/.config/msg/profiles/archczy/lightjunction identity show
msg --profile lightjunction tui
```

Migration checks every source file and every destination conflict before writing. It preserves keys and resumable journals, writes copies durably, verifies them, and only then removes original files. Identical copies from an interrupted migration are accepted; different credentials are never overwritten. Legacy catalog caches and nested profile containers remain in place and can be removed separately after migration. The default legacy `~/.config/msg/client.json` profile migrates automatically on first use. Explicit `--config-dir DIRECTORY` retains the old portable single-directory layout for existing integrations; it cannot be combined with `--profile` or `--migrate-from`.

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

Arch, Debian, and RPM packages share this layout. Package files belong to the package manager; service data and Root CA private state survive upgrades. The network service cannot access `/var/lib/msgd-root`. Root initialization intent now lives beside the protected root key, rather than under `/etc`. Recovery quarantine markers now live in the persistent data directory; old configuration-directory markers remain recognized. Both markers at once fail closed. The hosting reader's explicit recovery-marker configuration must point to the writer's marker.

Explicit server storage paths must be absolute and cannot contain `..`. Custom configuration directories and isolated test installations remain supported. Missing staging paths derive from that installation's data location instead of silently using production storage. A custom configuration directory uses a sibling `<config-name>-root` directory for Root CA state. Existing `server.toml`, legacy embedded blob/repository layouts, and `/etc/msgd/root/key.json` remain compatibility inputs; migration must preserve their security boundaries.

## Audit coverage

The audit covers client state and all client helpers, service configuration and administrative defaults, root initialization, backup/restore and recovery promotion, worker and Git staging, atomic writes, sandbox mounts, native packaging, deployment templates, and verification scripts. `scripts/audit_filesystem_paths.py` lists the tracked filesystem call sites and deployment path declarations for repeatable review.

Atomic replacement temporaries intentionally remain in the destination directory so rename is atomic even across mounts. Resumable uploads remain persistent; they are not cache. Worker/Git scratch directories use their configured staging roots. Disposable self-tests honor the temporary-directory environment. `/proc`, `/dev/null`, system executable paths, TLS trust paths, and sandbox-internal `/output` are operating-system interfaces, not application storage defaults.

## Account removal

`msgd account archive SUBJECT_ID` requires the Root ceremony; `--allow-ssh` explicitly permits an OS-root SSH terminal for this command only. It presents an exact preview digest and requires the Root passphrase. It archives the user profile, revokes credentials, advances the authentication version, and records a Root-signed audit event. Authentication and SSH reject archived identities. Account, resource, and ledger history remain intact. Root/system identities, active Bank roles, and nonzero balances must be resolved before archival.
