# Hosting runtime boundary (#161)

## Runtime roles

| Role | Composition | Runtime secrets | Metadata / content | Startup work |
|---|---|---|---|---|
| `msgd serve` | Application, full registered operations and executor | Online CA, receipt and token keys | Existing write authority | Verify installation and sync changed release rules |
| `msgd hosting` | HostingRuntime, callback-free Registry, existing authenticator/authorizer | Public trust only; separate read-only DB credential | Allowlisted SELECT, READ ONLY transactions; Git/CAS reads only | Verify trust, current authority, schema accessibility and exact installed release; never migrate/sync |
| `msgd worker` | Existing Application and registered effect worker | Existing channel/tool/issuer dependencies | Existing controlled write/effect authority | Existing lifecycle; not narrowed by this change |

Hosting is a process/OS/DB capability boundary, not a sandbox for Python plugins.
The installed plugin order remains in `msg.plugins.install_registry`. Hosting uses
the identical schema, capability and operation metadata, discarding both handlers
and requirement closures. It holds no parent Application, issuer, signer, token
secret, cursor MAC, OperationExecutor, Valkey signal or outbound sender.

`GitContentReader` binds the existing streamed Git/CAS read methods without a writer
instance or staging path. PostgreSQL uses the existing canonical transaction/session
implementation without initialization. The wrapper rejects write transactions; each
transaction also rejects a superuser, inherited role, owner/write/CREATE privileges,
unneeded table reads and callable application SECURITY DEFINER routines. This checks
configuration rather than repairing it. READ ONLY alone is not a role boundary.

## Separate deployment configuration

Review `deploy/msgd-hosting.service`, `deploy/hosting.example.toml` and
`deploy/hosting-role.sql`. Use a distinct `msgd-hosting` OS user and `msgd_hosting`
database login. The host config contains the same public service URL, plugin list
and content locations but **not** a copy of the writer DSN or mail/token keys.
A PostgreSQL service entry `msgd-hosting` plus a reader-owned 0600 pgpass/peer mapping
supplies the dedicated database credential. Protect those files and the config.

The SQL template grants only SELECT on schema_version, resources, revisions,
identities, memberships, credentials, certificates, settings, share_grants,
share_grants_v2, dm_conversations and system_sources. It does not grant table
ownership, sequence use, CREATE, role membership or token delivery/vault/job/ledger
reads. Existing PUBLIC grants must be reviewed separately; the template does not
revoke other applications' permissions. The runtime refuses an overpowered login.
New schema/authorization dependencies require review plus regression coverage before
adding a table. Column/row data returned by public HTTP still uses current authority.

Do not copy a possibly stale public trust file or recovery marker. Link the public
`trust/root.json` to the installed public trust, keep its directory traversable, and
set `hosting.recovery_marker` to the writer's real `recovery-drill.json` path. The
hosting account must be able to stat the marker's parent without reading writer
configuration or secrets. The sample marker is `/etc/msgd/recovery-drill.json`.
Missing/untraversable parents fail closed. An observed marker latches quarantine
until restart, even if someone subsequently removes it; the database gate is also
checked for every request. Root rotation requires restarting readers after the
approved trust update. The role cannot clear either recovery condition.

## Existing and future content permissions

Content sharing is **opt-in**: `[hosting] content_group_read=false` remains the
writer default. Do not simply add hosting to the writer's private group.

After stopping every writer, review `deploy/prepare-hosting-content.sh` and run it
as an OS administrator only for the standard paths it names. This is an explicit
permission migration, not an automatic startup action. It grants the dedicated
`msgd-content-read` group traversal and read access only to internal content and
CAS; it does not copy secrets, start services or change database grants. It refuses
symlinked content. Review nonstandard layouts manually rather than widening paths.
The database, service keys, repositories and transfer staging remain out of scope.
All future writer processes need the new supplementary group after restart.

Then explicitly set `content_group_read=true` in the **writer** configuration. New
content/index/CAS files are group-readable but not group-writable or world-readable;
setgid content directories retain the read group's ownership. New Git repositories
use `--shared=0640`; existing repositories must already have the reviewed mode.
Binary staging remains private; only verified publication adopts the destination's
read group. A private existing installation fails with `content_sharing_not_prepared`
instead of silently chmod'ing history. Backups/restores must preserve these modes
and restore quarantine; repeat the operator review before read service promotion.

## Transport and acceptance

This does not change the current **same-origin** contract. The edge proxy sends only
hosted-content paths to the loopback hosting listener; ordinary API/operation routes
remain on the main service. Response-level CSP sandbox, no same-origin allowance,
no-store/nosniff, signed private preview, current revocation, history, HEAD/Range/304
and explicit publish/rollback semantics are preserved. Unknown host/path/methods do
not open a write route. Startup never generates a key, creates content directories,
repairs missing tables or publishes release revisions.

`tests/test_hosting_runtime.py` exercises the actual SELECT-only PostgreSQL login,
an OS-unreadable service-key directory, unchanged business facts, denied writes,
missing/stale prerequisites and current authorization/quarantine. Permission tests
check new text/binary/index publication under umask 0077 and refusal to alter an
existing private installation. Existing transport/CLI checks and full four-shard
CI remain required. Evidence and outstanding checks: `HOSTING_RUNTIME_PROGRESS.md`.
Green isolated CI is not a production deployment or completion of #80/#84.
