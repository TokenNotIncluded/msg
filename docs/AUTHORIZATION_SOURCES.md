# Current authorization sources (issue #76)

Every adapter uses OperationExecutor and the current Authorizer. A signature,
resource ID, cursor, historical revision or role name is never a grant by itself.
Credential ceilings, entry restrictions, live security-parent traversal and
special type/state gates apply before an alternative source can authorize access.

| Source | Live conditions | Losing the source |
|---|---|---|
| Owner/mode | Current owner, mode, active security-parent chain | Owner/mode/state change takes effect immediately |
| Organization/group | Active organization and matching active membership; `public` remains virtual | Leave, removal, archived/missing organization or mismatched restored row denies that source |
| Topic governance | Current membership policy; bans on every ancestor topic constrain writes | A nested topic cannot escape a parent ban |
| Certificate | Current issuer chain, grants, authority sources, scope and credential ceiling | Revocation, expiry, source loss or narrower scope is rechecked |
| ShareGrant@2 | Ordinary shareable resource; current owner at root, active user/group, exact read operation and empty constraints | Expiry/revocation/owner change removes this source, not an independent one |
| Reshare | Same resource, fixed source chain, each parent currently allows reshare; child expiry no later than current parent | Cycles, missing parents, shortened expiry, unknown operations/constraints fail closed |
| ShareLink | Disabled by default; live owner, source credential and resource checks | Token is not public access; expiry/revocation/owner or credential changes deny it |

The first supported ShareGrant@2 contract remains **read only, empty constraints**.
Unknown constraints and future operation names are rejected, not silently ignored.
This is not a general delegation evaluator. Containers, controlled/system facts,
DMs, SOUL/AGENTS personal roots, Todo and preview resources are not shareable.
Grant creation, source-chain validation and shared/link reads use one boundary
predicate. Sharing a child does not open its parent or another attachment.

Ordinary read/write capabilities replace only their named checks. A group or
Topic role never implies CA, system, root, purge, chown or credential-management
rights. Topic bans constrain writes even when an outer source permits them;
independent read sources remain independent. Losing a group role does not remove
an otherwise active membership. Owner transfer and leaving are separate facts.

## Regression evidence

`tests/test_authorization_matrix.py` uses one nine-source table: owner, mode,
inherited traversal, organization member/maintainer/owner, scoped certificate,
user ShareGrant and group ShareGrant. Each source is established and removed by
real operations (with one explicit current-parent relocation fixture). It checks
current content, history and pinned revisions through HTTP, PathGET, GraphQL and
MCP, plus the actual CLI parser/dispatcher with its real signed client. It also
checks Search, Sync, Inbox and Outbox after revocation. Reused adapters may cache
transport descriptions, never positive resource authorization.

`tests/test_authorization_sources.py` covers archived organizations with stale
membership rows, independent source preservation, restored unknown operations
and constraints, contracted parent expiry, newly protected previews and ancestor
topic bans. `tests/test_authorization_recovery_boundaries.py` proves that a shared
post does not open an attachment; concurrent reads cannot retain access after a
completed revocation; ShareLinks work only through supported body transports;
and a real pg_dump/restore preserves denial of content/history/old revisions and
filters notifications. Restored workers remain disabled in a recovery drill.

These 20 new cases passed locally with real PostgreSQL 14.24, Git and signatures
on Python 3.13.5. That evidence does not replace the final commit's Python 3.15,
PostgreSQL 16, Valkey, full core suite, conformance and build CI.

The new matrix complements `test_share_grants*`, `test_share_links`,
`test_organization_governance`, `test_topic_governance`, `test_authorization`,
`test_linkset_diff` and `test_search_source_relation_filters`, including explicit
role boundaries, organization policy changes, Topic virtual-event privacy and
old search cursor revalidation. Reads do not repair stale facts or append
business events, jobs, audit records or revisions. No production data, Root
credentials, deployment configuration or signed CA ceilings are changed.
