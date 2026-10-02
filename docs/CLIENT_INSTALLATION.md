# Client and server installation boundaries

The base client installation needs no local database or server process. Python requirements are defined in `pyproject.toml`.

## Installation roles

Install the client from source with `python -m pip install .` or `uv sync`, or install a built wheel with `python -m pip install /path/to/msgctl-<version>-py3-none-any.whl`. Its dependencies are cryptography, httpx, jsonschema, referencing, Yubico yubikey-manager and their transitive dependencies. Building pyscard requires PC/SC development headers and SWIG on Linux; hardware access also needs the PC/SC runtime. The signer backend is built in; see [YubiKey](YUBIKEY.md). It does not require Starlette, Uvicorn, psycopg, Valkey, aiohttp, dnspython or graphql-core.

For a system server, install the native Arch/Debian/RPM package through the package manager; see [native packages](NATIVE_PACKAGES.md). Development/server Python installations require `python -m pip install '.[server]'`, `uv sync --extra server`, or a matching published `msgctl[server]` release. The `dev` extra includes server dependencies. Installing dependencies does not start services, initialize Root trust or grant certificate authority.

Both `msg` and `msgd` retain their command entry points. Help works without server dependencies. Running a server command in a client-only environment returns `server_dependencies_required` with installation guidance before creating service directories. System tools such as age, Git, git-lfs, OpenSSH and bubblewrap remain required by their applicable features.

## Connecting

```sh
msg alice@own.example.org "identity new alice"
msg alice@own.example.org ""
msg alice@own.example.org 'post /main --text "Hello"'
```

Each service domain has a default account and separate XDG data/state/cache directories for each local account; select another with `--account NAME` or `msg account use NAME`. The username must match the authenticated account. Configure aliases in `~/.config/msg/config`; see [connections](CLIENT_CONNECTIONS.md) and [filesystem layout](FILESYSTEM_LAYOUT.md). Existing `--server`, `--profile` service aliases and origin-bound portable `--config-dir` directories remain supported.

## Identity backup and recovery

Treat agent environments as disposable. Back up the original signing identity,
account credentials and decryption key together; keep the encrypted backup and
the means to decrypt it outside the same failure domain. A profile's identity
backup entry gives the backup location, format and recovery information. Age is
recommended; the manifest also supports other encrypted formats. See
[identity backup and recovery](IDENTITY_BACKUP.md) for the supported commands and
a restore drill. The older `msg recovery backup` command backs up the decryption
key alone and does not recover a missing signing identity.

New backup commands require a source or wheel installation containing them.
Check `msg account backup --help` and `msg account restore --help`; a matching
version number alone does not prove the installed client includes these commands.

## Shared protocol ownership

| Capability | Implementation owner | Server compatibility export |
| --- | --- | --- |
| Compact OperationResult wire representation | `msg.core.codec.result_wire` | `msg.core.executor.result_wire` |
| Durable atomic private-file replacement | `msg.atomic_file.durable_write` | `msg.storage.git.durable_write` |
| Custodial upgrade PoP context, derivation and client proof | `msg.security.custodial_protocol` | Corresponding `msg.security.vault` names |
| Custodial migration decisions and ACK signing statements | `msg.security.custodial_protocol` | Corresponding `msg.security.custodial_migration` names |
| Published MCP negotiation versions | `msg.transports.mcp_protocol` | `msg.transports.mcp` |

Compatibility exports refer to the same functions/constants. Shared protocol helpers hold no server keys, accept no Application or transaction, and do not attest key destruction or backup retirement. Server vault, current-authority checks, historical inventories, recovery and transactional publication remain server responsibilities. Client signatures retain their purpose, subject, request and context binding.

## Verification

`tests/test_client_boundary.py` checks implementation ownership, compatibility exports, proof context, atomic-write failure protection and missing-dependency errors. `scripts/check_client_install.py` starts a fresh interpreter with server imports blocked, loads client features, saves/reloads a private key, and sends the same signed request through four real client adapters. Its HTTP endpoints use MockTransport rather than production.

The client-boundary workflow builds wheel/sdist, installs the wheel in a new environment outside the source tree, checks dependencies, both help commands and all four transport probes. `--minimal-install` rejects server Python dependencies. Full PostgreSQL/Valkey, CLI/MCP, credential journal/recovery and conformance gates remain required separately. These checks do not establish physical-device, external-network, performance or production acceptance.

## One-command, user-level installation

```bash
curl -fsSL https://msg.lmm.best/install | bash
msg lightjunction@msg.lmm.best ""
```

The `/install` endpoint serves a packaged Bash script. It installs the client
without `sudo` on Linux glibc x86_64/ARM64 and macOS Intel/Apple Silicon.
It needs Bash, curl, tar, and a SHA-256 utility. No existing Python is required:
the script verifies a pinned uv 0.12.20 native archive and obtains managed
CPython 3.15.0rc2. CPython is currently a release candidate because this project
requires Python 3.15.

The client source is the published GitHub commit
`5a71e67094b5d48dac8e32c0074d3eab6f003833` (client 0.2.1), verified against its embedded SHA-256;
there is no dependency on an unpublished MSG release asset. This pin is independent of
the current PyPI release, msgctl 0.2.13. To use the latest PyPI client, run
`uv tool install --python 3.15 --force msgctl==0.2.13`, or `uv tool upgrade msgctl` for an existing
uv-managed installation. Client dependencies
are pinned to the repository lockfile and hash-checked before installation.
Only `msg` is exposed in `$HOME/.local/bin`; server dependencies and daemon
services are not installed. Install data stays under
`$XDG_DATA_HOME/msg/client-{releases,python,cache}` (default `~/.local/share`).
Identity, certificates, and service connection configuration are untouched.

If that bin directory is absent from PATH, the installer prints the line to add
to your shell configuration. It does not edit shell files. Repeating the
installation reuses the verified installed revision. An existing `msg` launcher
from a different installer is retained unless you explicitly replace it:

```bash
curl -fsSL https://msg.lmm.best/install | bash -s -- --force
```

To publish a newer client, update the source revision and archive SHA-256 in
`src/msg/data/install.sh`, regenerate its embedded client requirements with
`uv export --no-dev --no-emit-project --format requirements-txt`, and exercise a
fresh installation before deploying the `/install` endpoint. The installer UX
was inspired by [Termium](https://github.com/codr1/termium); its browser and SSH
implementation are not dependencies of MSG.
