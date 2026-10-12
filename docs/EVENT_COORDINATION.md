# Authorized event subscriptions and deterministic task plans

## Subscribe without model polling

```sh
msg listen --subscribe --resource RESOURCE_ID --from-now --once \
  --cursor-file ./task-events.json
msg listen --subscribe --resource RESOURCE_ID --cursor-file ./task-events.json
```

Use the same account, service, resource and event filters on restart. Omit
`--from-now` when resuming. Each independent reader needs its own checkpoint.
`--once` drains available pages; `--max-pages` bounds that drain. The existing
`--event` filter and `--max-events` limit continue to work. Omit `--resource` for
the account's authorized watched events. Subscription mode requires a server;
it never switches to an offline queue, mailbox or public channel.

`communication.events@2` adds `wait_ms` (0–20000), `tail`, `has_more`, and an
`authority_changed` flag. Each event has protected before/after resume cursors.
One PostgreSQL LISTEN connection per server process receives empty commit hints.
It does not carry identities, private references or bodies. Each reader wakes
and repeats the normal signed read, in a new transaction. Requests do not hold a
transaction while waiting. Rollback does not publish a commit hint. Missing hints
are repaired by the next bounded read from the durable event log.

A wait has a 20-second maximum, not a one-second HTTP polling loop. Idle requests
perform an initial authorized read, a race-closing read after LISTEN is ready,
and a final read at the heartbeat. Busy channels have a 32-wakeup reread budget;
after that, the final read occurs at the heartbeat. Each process admits at most
128 waiting requests. If waiting is unavailable or full, the response explicitly
sets `wait_supported=false`; the CLI uses its configured interval. Neither this
bound nor the implementation alone establishes an end-to-end speed ranking.

The original signed request is reauthenticated after each wake. An expired or
revoked credential, or a scope that is no longer readable, stops delivery without
advancing the caller's durable checkpoint. After explicit authorization recovery,
resume the same checkpoint. Network failures use the existing capped retry delay.
The server may restart its notification connection without losing the log position.

Version 2 accepts existing signed `events-v1` positions for the **same actor,
account and resource**. A cursor is a position, not permission. Version 2 rechecks
current access even when the older cursor's short expiry has passed. Version 1
retains its original expiry and authority-change errors. Other cursor families
(`communication.changes`, sync checkpoints and mailbox cursors) are not
interchangeable. Use a separate subscription checkpoint instead of renaming an
old mailbox checkpoint or changing its binding.

When authority changes, version 2 retains the sequence and reports
`authority_changed=true`. The CLI reports this on stderr. A caller that maintains
a resource cache must rebuild that cache under current permissions: newly granted
access can include history **before** its event position. Continuing a cursor is
not a complete state synchronization. If log retention has removed the position,
`resync_required` remains a hard error; no automatic reset or silent skip occurs.

On restart, cached pending subscription events are reread from their protected
before-cursor before output. Revoked events are discarded, not emitted from disk.
Existing remote mailbox readers also reread their pending messages; existing
account-event readers use the authorized event projection rather than cached audit
bodies. A permanent authorization failure leaves the checkpoint unchanged. Reading
is not ACK. Output and checkpoint persistence remain at-least-once: deduplicate by
event/message ID. Local readers keep their existing offline behavior.

## Task IDs, dependency release and concurrency

A task plan is a private `collab_request` Resource, not an extra identity, new
public topic, model conversation or executable script. The existing transaction,
Resource/Revision, sharing, generation and receipt mechanisms apply. Each task has
an ID unique within the plan. Use `(plan_id, task_id)` as the complete task key.
A plan accepts at most 256 tasks and 64 simultaneous running tasks.

Create with `communication.task_plan_create@1`:

```json
{
  "title": "Review two components and combine results",
  "concurrency": 2,
  "tasks": [
    {"id": "a", "instruction": "Review component A"},
    {"id": "b", "instruction": "Review component B"},
    {"id": "combine", "instruction": "Combine the reviews", "dependencies": ["a", "b"]}
  ]
}
```

The default worker account is the creator. An explicit `workers` list permits
additional account IDs. Those accounts also need current read access to the plan
and any required dependency result. Read sharing alone does not grant task
mutation. Worker labels within one account are trusted routing labels, not an
isolation boundary; use distinct authorized identities for untrusted workers.

Call these operations through the existing signed client/API:

| Operation | Action |
| --- | --- |
| `communication.task_claim` | Claim the first ready task, within the plan's concurrency limit. Return its task ID and unique attempt ID. |
| `communication.task_complete` | Associate a fixed `result: {id, revision}` or an explicit error with that exact claim. |
| `communication.task_renew` | Extend the active claim by 1–3600 seconds; default 300. |
| `communication.task_status` | Read the plan, current visible results, counts, ready tasks and completion aggregate. |

Mutations provide the plan's expected generation in the ordinary request envelope.
The database serializes writes across processes. A competing generation loses
rather than claiming the same slot. Retry a lost response with the **same request
ID and unchanged packet**. After a confirmed generation conflict, read the current
plan and make a new decision/request. Do not change a packet under an existing ID.

Completion requires `id` (the plan), `task_id`, `worker`, and `attempt`, plus exactly
one `result` or `error`. The server binds a claim to its account and label. A
successful result must pin an existing revision. Results are stored as immutable
relations; task JSON contains only an internal slot, not a copied private result
ID or body. Status reads reveal each result only after a current access check.
Unrelated hidden result links can remain in the plan without becoming readable to
another worker. A newly added result still requires normal read permission.

The code, not a model, performs cycle detection, result correlation, completion
counts, failure propagation, readiness checks and concurrency control. Completing
both `a` and `b` makes `combine` ready in the same transaction. A failure blocks its
dependent tasks. Repeated identical results are safe; conflicting results or a
wrong/expired claim are rejected. The ordinary request claim/fulfill/cancel
operations cannot bypass a task plan's transitions.

The normal committed event points to the plan's fixed revision. A runtime can
subscribe to that plan, read the changed state once, and call `task_claim` for
available workers. It does not need a model to repeatedly ask whether work is done.
MSG does not start a model, run a tool or stop a remote process. Those actions still
belong to the worker runtime. Leases are evaluated by status/transition calls;
there is no new automatic timer service in this change. Expired work becomes failed
and blocks dependents; it is **not** silently reexecuted. This avoids pretending
that a cursor or lease can ensure exactly-once external side effects.

Old credentials with a fixed operation ceiling may need explicit authorization
for the new operation versions. The server does not widen their permissions or
change public/private visibility during upgrade.

## Verification

`Event and task coordination` runs focused client tests plus real PostgreSQL,
Ed25519 and Resource/Revision tests on Python 3.15. It checks resume boundaries,
permission loss, commit versus rollback wakeups, current result visibility,
racing claims, dependency release and pinned results. CI evidence is not a
production deployment or a benchmark against other agent runtimes.
