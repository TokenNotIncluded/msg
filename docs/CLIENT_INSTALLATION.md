# Client and server installation boundaries

The base client installation needs no local database or server process. Python requirements are defined in `pyproject.toml`.

## Installation roles

Install the client from source with `python -m pip install .` or `uv sync`, or install a built wheel with `python -m pip install /path/to/msgctl-<version>-py3-none-any.whl`. Its dependencies are cryptography, httpx, jsonschema, referencing and their transitive dependencies. It does not require Starlette, Uvicorn, psycopg, Valkey, aiohttp, dnspython or graphql-core.

For a system server, install the native Arch/Debian/RPM package through the package manager; see [native packages](NATIVE_PACKAGES.md). Development/server Python installations require `python -m pip install '.[server]'`, `uv sync --extra server`, or a matching published `msgctl[server]` release. The `dev` extra includes server dependencies. Installing dependencies does not start services, initialize Root trust or grant certificate authority.

Both `msg` and `msgd` retain their command entry points. Help works without server dependencies. Running a server command in a client-only environment returns `server_dependencies_required` with installation guidance before creating service directories. System tools such as age, Git, git-lfs, OpenSSH and bubblewrap remain required by their applicable features.

## Connecting

```sh
msg alice@own.example.org "identity new alice"
msg alice@own.example.org ""
msg alice@own.example.org 'post /main --text "Hello"'
```

Each service domain selects one identity and its XDG data/state/cache directories. The username must match the authenticated account. Configure aliases in `~/.config/msg/config`; see [connections](CLIENT_CONNECTIONS.md) and [filesystem layout](FILESYSTEM_LAYOUT.md). Existing `--server`, `--profile` service aliases and origin-bound portable `--config-dir` directories remain supported.

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
