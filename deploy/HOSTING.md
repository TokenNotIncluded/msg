# Dedicated read-only hosting role

The `hosting` command is a separate **process role**, not a Python sandbox or a
second authorization implementation. It serves sandboxed ` /@name/web/ ` routes
on the service origin. `public_web_origin` must equal `service_url`; a separate
listener port does not establish a second browser origin.

| Role | Database/content | Service private keys | Writes/jobs |
| --- | --- | --- | --- |
| `serve` | Existing read/write store | Online/receipt/token material | Common executor and outbox |
| `worker` | Existing read/write store | Existing dependencies, unchanged here | Durable jobs/maintenance |
| `hosting` | Dedicated SELECT login; read-only content trees | None | No executor, issuer, migration, rule publication, staging or outbox |

Hosting uses the same registry installer order, schemas, authentication,
certificate validation and current authorization as the writer. Executable
operation and requirement callbacks are removed from both operation maps and
plugin manifests. No `Application`, signer, derivation secret or credential
response hook is retained.

## Administrator provisioning

Use separate OS user/group `msgd-hosting`, content-only group `msgd-content`, and
`/etc/msgd-hosting`. Add the writer and reader to the content-only group, **not**
the reader to the writer's general group. Never copy the whole writer config:
its DSN or delivery credentials can confer writes even when keys are hidden.
Copy only the verified public trust anchor to `/etc/msgd-hosting/trust/root.json`.
Keep all Root-private paths and `/var/lib/msgd/service` inaccessible.

In a dedicated installation database, create a login with
`NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS`, no role
memberships and no owned objects. Grant CONNECT, USAGE on the service schema,
and SELECT only on these existing tables:

```text
schema_version resources revisions identities credentials certificates memberships
settings csrs share_grants share_grants_v2 topic_bans dm_conversations dm_blocks
system_sources
```

No table/column writes, sequence USAGE/UPDATE, schema creation, database
CREATE/TEMPORARY or executable SECURITY DEFINER routines are allowed. PUBLIC
privileges still apply: a DBA must remove PUBLIC CREATE/TEMPORARY where present
and explicitly preserve legitimate writer requirements. Do not blindly change
PUBLIC privileges in a shared database. A read-only transaction default is not
sufficient; startup checks effective rights, ownership and SET ROLE paths. It
never automatically creates a role or initializes/migrates a database.

Minimal hosting config:

```toml
[server]
service_url = "https://msg.lmm.best"
public_web_origin = "https://msg.lmm.best"

[storage]
postgres_dsn = "service=msgd_hosting"
content = "/var/lib/msgd/git/content"
blobs = "/var/lib/msgd/blobs/sha256"
staging = "/var/lib/msgd/transfers/staging"
service_keys = "/var/lib/msgd/service"
```

The last two paths are compatibility settings only; hosting never opens or
creates them. Provision a reader-only libpq connection service/password outside
the repository in root-owned files readable only by `msgd-hosting`. Set
`PGSERVICEFILE`/`PGPASSFILE` explicitly in the unit for a non-default location.
There is no sample password or deployment automation in this change.

## Filesystem isolation and live publication

The systemd unit makes config and content read-only and masks writer config,
service keys, transfers, repositories and Root material. Do not add
`ReadWritePaths` or use the writer UID. Grant traversal/read access only to the
content/index/private-Git and binary trees, never all of `/var/lib/msgd`.

Provision existing content directories to owner-writable, group-readable setgid
mode `2750`, group `msgd-content`, with default read/traverse ACLs for newly made
directories; files are `0640`. Keep staging owner-only. Configure only the private
content Git repository with `core.sharedRepository=0640` so new Git objects/refs
stay group-readable but not group-writable. A newly configured reader requires
an explicit review/provisioning pass for existing content; startup does not
chmod/chown anything.

Content publication opts into `0640` **only** when its destination directory is
both setgid and group-readable. Binary publication also uses the destination
content group rather than an unrelated staging group. Without this opt-in,
index/binary files keep `0600`. The shared `durable_write` default stays `0600`,
including when writing secrets inside a setgid directory. No world-readable
permissions or key-permission changes are introduced. Verify both newly
published text and binary content, not just a permissive copy of old files.

Git trusts only the configured writer-owned private content repository, never
`safe.directory=*`. Python attributes are not a sandbox; the separate UID, group
permissions, systemd mount namespace and actual SELECT role are independent
mandatory controls.

## Startup, upgrades and recovery

Hosting validates public trust, current root identity, schema version, persisted
quarantine and installed release-rule records/content before serving. Missing
trust, old rules, incomplete storage or an overprivileged DSN fail startup.
Prepare upgrades through the authorized writer path, then restart hosting;
there is no anonymous fallback or automatic repair.

Every request rechecks current authority before ETag/HEAD/Range. Persistent DB
quarantine protects separate config directories. A `recovery-drill.json` file or
dangling symlink in the hosting config also denies reads; deleting that marker
cannot clear the database gate. Root rotation requires installing the verified
new public trust and restarting; old processes reject a changed active root.
This command never performs recovery promotion.

## Verification

`Hosting read-only role` runs real PostgreSQL/Git tests on GitHub Actions. The
Linux isolation test needs `sudo` and `setfacl` (package `acl`). A disposable UID
65534 reader must fail service-key reads and business-file writes while reading
public pages and signed previews published after permission provisioning. It
performs no post-publication chmod repair. ACL/key revocation, cached/partial
reads, quarantine and elevated-DB-role failures have separate tests. Full
four-shard node-ID, conformance and wheel/sdist gates remain required.

References for deployment semantics: PostgreSQL 16 role membership and privileges,
Git `core.sharedRepository`/`safe.directory`, and systemd.exec filesystem sandboxing.
