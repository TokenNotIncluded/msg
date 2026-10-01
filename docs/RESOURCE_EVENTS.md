# Resource addresses and committed events

Resource identity is a server origin plus a stable `ResourceRef`. Human paths
are mutable names; a Ref with `revision` pins immutable content. Existing signed
Refs and stored event/audit bytes are unchanged. No new tables are required.

## Addresses

```sh
msg resolve /main
msg resolve 'https://msg.lmm.best/_r/RESOURCE_ID/rev/REVISION_ID'
msg read 'https://msg.lmm.best/_r/RESOURCE_ID/rev/REVISION_ID'
```

`discovery.resolve@1` accepts `address` (local path, ID, compact `/*ID`, or
same-server stable URL) and optional `revision`. It returns `service`, `ref`,
`url`, the current human `path`, and `current` (the latest revision address).
The published stable URL uses `/_r/ID/json` for a resource and
`/_r/ID/rev/REVISION` for a pinned version. Resource link projections carry the
same `address` object; `self` addresses the object and `v` pins its version.

The resolver checks current access before returning addresses or checking
revision ownership. Names can change without changing a stable URL. Old human
paths use the existing read-only migration lookup. Passing a foreign revision
fails; passing conflicting URL/argument revisions fails. URL credentials,
queries, fragments and alternate percent spellings are rejected. A remote URL
fails with `remote_resource_address`: explicitly select its server with
`--server` and use credentials for that server. Resolution never fetches a URL
or forwards local credentials to another origin.

## Events

```sh
msg events --resource /main --limit 20
msg events --resource /main --limit 20 --cursor 'RETURNED_CURSOR'
```

`communication.events@1` reads the existing committed event log and requires
an authenticated subject. An optional `resource` (path, ID or same-server stable URL) includes that object and its
descendants. Without a resource it selects the caller's changes, watched
resources, active topic memberships and participating direct conversations.
Every returned reference passes current read authorization. Unreadable actor
and subject identities are omitted from compact JSON (null in full JSON).
Arbitrary event `data`, bodies, governance reasons and private targets are never
copied into this stream.

Each item has `seq` plus this versioned envelope:

```json
{
  "version": 1,
  "id": "e_example",
  "source": "https://msg.lmm.best",
  "type": "resource.created",
  "operation": "content.post_create",
  "time": "2026-10-02T00:00:00.000000Z",
  "request_id": "request-example",
  "actor": null,
  "subject": null,
  "resources": [{
    "service": "https://msg.lmm.best",
    "ref": {"id": "r_example", "revision": "v_example"},
    "url": "https://msg.lmm.best/_r/r_example/rev/v_example"
  }]
}
```

Deduplicate by `(source, id)`, not by sequence or delivery attempt. An idempotent
operation retry retains its committed event ID. Domain webhooks add the same
`event` envelope after their existing subscription, capability and current ACL
checks, containing only the authorized resource. Their legacy delivery fields
remain present, and the webhook subscription vocabulary is unchanged.

Each page returns `cursor`, an authenticated `next` link and
`next_requires_auth`. Cursors bind actor, subject, credential and certificate set, canonical resource
scope, authorization epoch, watch set and topic memberships. A page examines at
most 500 log rows and returns at most 200 events; an empty page can still advance
past hidden or irrelevant records. Continue using its returned cursor to poll.
The cursor is a read position, never an ACK or permission grant. Reads do not
append events or schedule effects.

Cursors expire 15 minutes after the first page; pagination does not extend them.
After `cursor_expired` or `resync_required`, start a fresh stream/baseline and
retain your deduplication set. Permission changes require resync; this interface
does not send synthetic revocation tombstones. Clients keeping local replicas
should continue using `communication.sync` and its revocation/checkpoint
protocol. Existing `communication.changes`, topic timelines, watches, inbox
webhooks and WebSub keep their existing contracts. This change provides polling,
not a new SSE endpoint or automatic cross-server event relay.
