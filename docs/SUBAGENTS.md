# Private subagents and asynchronous event listeners

`@username#bot1` and `@username#bot2` are labels within one account, not additional
registered users. They share the account's authority and local OS user. Labels
route internal messages; they are not an isolation boundary between untrusted
agents. No new identity, CA permission or public post is created.

For independent keys, expiring permissions, revocation or a one-success task, use
[delegated task identities](DELEGATED_IDENTITIES.md) (`@alice~<suffix>`). A `#bot`
label and a task identity can be used together; the label itself never limits authority.

The examples below assume an existing MSG account called `alice`. Use the same
profile/server selection for every participating process. First use with an older
client identity may require `--username alice`; this name is remembered locally.
New registrations retain the username for offline use.

## Local collaboration, including offline operation

```sh
msg --offline --username alice agent create bot1
msg --offline --username alice agent create bot2
msg --offline --username alice --agent bot1 agent send '@alice#bot2' 'Review the patch and reply.'
msg --offline --username alice --agent bot2 agent inbox
msg --offline --username alice --agent bot2 agent send '@alice#bot1' 'Reviewed: tests pass.'
```

These commands never connect to MSG. Private SQLite state lives under the client's
account/origin data directory, with directory permissions `0700` and files `0600`.
Processes using that same directory can exchange messages. Without an existing
login, `--username alice` also permits a purely local label namespace; this does
not register or reserve `alice` on any server.

## Wait in the background

```sh
msg --offline --username alice --agent bot2 listen \
  --cursor-file ./bot2.cursor.json > ./bot2.events.jsonl &
```

The command stays running. Each matching event is one JSON line, flushed as soon
as it is read. No empty page or polling response is written to stdout. The queue
is checked once per second by default; this is a blocking CLI backed by polling,
not an SSE or WebSocket connection.

`--event subagent.message` filters event types. `--from-now` skips old events when
starting with a **new** cursor file; omit it when resuming an existing checkpoint.
`--once` drains available events and exits; `--max-events 1` waits for one matching
event and exits. `--interval 0.5` changes the polling interval. Stop with Ctrl-C or
SIGINT.

For a task runner that needs one result before continuing, omit `&` and wait for
one event:

```sh
msg --offline --username alice --agent bot2 listen --max-events 1
```

The process waits silently while the mailbox is empty and exits successfully
after writing one matching event. Another process can send the task or result
while it waits. For continuous asynchronous cooperation, keep the background
listener above running and consume its JSONL output incrementally; do not wait
for that process to exit before reading it.

Start each worker's independent fresh-tail listener before reading its initial
task reference, so `--from-now` cannot skip follow-up messages sent during setup.

A cursor belongs to its server, account, mailbox and event filters. Each listener
has its own checkpoint by default; specify different files for independent
readers. Simultaneous use of the same file fails with `cursor_in_use`. Reading
never marks an entire account's messages consumed. A crash between output and
checkpoint persistence can replay an event: deduplicate by message `id`.

## Remote private mailboxes

```sh
msg --agent bot1 agent create bot1 --remote
msg --agent bot2 agent create bot2 --remote
msg --agent bot1 agent send '@alice#bot2' 'Run the integration checks.' --remote
msg --agent bot2 listen --remote --cursor-file ./remote-bot2.cursor.json
```

Each participating machine needs explicit authorization for the **same** account.
The suffix does not authorize a new machine: use the existing account authorization
flow or an explicitly scoped credential. Do not copy a master private key into an
agent prompt or expose tokens in URLs.

Remote messages use existing private resources beneath
`/@alice/files/agents/<bot>`. Setup requires the account's existing file creation,
chmod and topic-configuration permissions; receive uses `communication.changes`
and authorized file reads. Topics are `0700`, message files `0600`, and the client
rejects a namespace that belongs to another account or is publicly accessible.
No public message, profile or feed entry is created. Local and remote queues are
separate; there is no automatic sync or silent fallback between them.

Each inbox call freshly reads the account identity, then checks the three private
namespace directories and the label configuration. A supporting HTTP adapter
combines those four checks in one existing read query, with a separate signed and
authorized envelope for each read. No identity, ACL or configuration is cached.
A warm empty poll needs three HTTP requests instead of six; each message body
still needs its own authorized read. Unsupported or temporarily unavailable read
queries use the original serial checks. Permanent authority or privacy failures
stop before change-stream reads; a transient sibling cannot hide them. Archived
label history stays readable while a physically archived namespace is rejected.
This reduces per-poll overhead; it does not change the bounded global event scan
or add a mailbox index.

A remote listener retries interrupted reads, including bounded non-JSON responses
with HTTP status 502, 503 or 504 during gateway or server restarts. The operation
must be declared `read` by the service transport description; signed POST reads
qualify, and a GET operation with a write effect does not. The transport description
and validated batches of reads also qualify. Malformed successful responses,
responses declared as JSON but failing to parse, strict JSON and result-envelope
failures, decoded permanent authorization failures, redirects and oversized
responses still stop the listener. Resolve those failures explicitly rather than
restarting blindly. The existing handling of valid JSON gateway errors is unchanged.

The existing client retries reuse the same signed packet and request ID. After
those attempts are exhausted, the listener retries the unchanged durable cursor
with exponential waiting capped at 30 seconds until recovery or cancellation.
This cap bounds each wait, not the total outage duration or number of attempts.
No error page is emitted as an event, and a failed read does not advance the
checkpoint or ACK a message. Successful recovery remains subject to the normal
at-least-once output/checkpoint behavior described above. This recovery rule does
not add retries to dispatched transaction or external-effect operations; their
existing network retry and uncertain-result handling remain unchanged. Expired
credentials, session refresh failures or `resync_required` can still require
explicit credential recovery or a deliberately new cursor.

`msg agent archive bot2` (or `--remote`) stops future sends involving that label
and preserves history. Account authorization changes can invalidate a remote
cursor with `resync_required`; choose a new cursor and replay deliberately.

Keep the returned cursor when checking an inbox repeatedly. A fresh inbox starts
at the account's event history; `tail=True` in the client API, or `listen
--from-now` with a new checkpoint, deliberately starts at the current end.
Newer servers attach authorized current-parent hints and protected event resume
cursors to the existing change-stream output. The client can skip unrelated
message-body reads, fetch larger internal pages without exceeding the requested
message limit, and obtain the tail without reading old message bodies. Older
servers retain the original output. These additions preserve the published
operation inputs and short codes; they do not add a server-side mailbox index, so
a cold scan can still require linear database work.

The change stream now scans at most 64 stored events per page and yields during
current permission checks. It can return an empty or short page with
`has_more=true`; resume its protected `sync_cursor` to continue. Remote inbox
calls scan at most eight pages before returning their progressed cursor and
`has_more`, so even a long sparse history does not make one call drain the entire
account. Keep continuing while `has_more` is true. A legacy server without the
new output hints retains the original page-size inference; legacy tail scans
still run to the end so they do not silently select an earlier position.
Older clients can explicitly resume the same protected cursors, but clients that
infer completion from a short page can pause before reaching the current end on
newer servers. Update those readers to follow `has_more` rather than treating an
empty page as proof that the account history is drained.

## Existing account events

```sh
msg listen --event communication.dm_send --cursor-file ./account.cursor.json
```

Without `--agent`, `msg listen` reads the account's existing authorized change
stream. It does not create a resource subscription or expand access. Continue to
use the existing watch commands for resources you want to subscribe to.

The CLI and JSONL format do not depend on a particular agent software. An agent
that can run commands or read a file can participate. The tested software and
network environments, including external-model failures, are recorded separately
in the [release acceptance evidence](SUBAGENT_ACCEPTANCE_20261001.md).
