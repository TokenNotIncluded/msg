# Private subagents and asynchronous event listeners

For programming tasks, use the short task, pinned patch and validation handoff
conventions in [MSG programming collaboration](AGENT_PROGRAMMING.md).

`@username#bot1` and `@username#bot2` are labels within one account, not additional
registered users. They share the account's authority and local OS user. Labels
route internal messages; they are not an isolation boundary between untrusted
agents. No new identity, CA permission or public post is created.

For independent keys, expiring permissions, revocation or a one-success task, use
[delegated task identities](DELEGATED_IDENTITIES.md) (`@alice~<suffix>`). A `#bot`
label and a task identity can be used together; the label itself never limits authority.

The examples below assume an existing authenticated MSG username `alice`. Use
the same account and service selection for every participating process; local
account labels selected with `--account NAME` may differ from the server username.
Remote mailboxes carry collaboration between authorized machines. Local queues
are a separate offline option, not an automatic fallback.

## Remote private mailboxes

```sh
msg --agent bot1 agent create bot1 --remote
msg --agent bot2 agent create bot2 --remote
msg --agent bot1 agent send '@alice#bot2' 'Run the integration checks.' --remote
msg --agent bot2 listen --remote --cursor-file ./remote-bot2.cursor.json
```

Clients whose `msg agent send --help` lists `--receipt` can add it to a remote
send to return the saved message reference without echoing the body. Keep
`--message-id ID` for retries. This is a delivery reference, not evidence that the
recipient read the message; local queues reject `--receipt`.

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

Each inbox call freshly reads the account identity, checks the private namespace
and label configuration, and authorizes each message-body read. A supporting HTTP
adapter batches namespace checks with a separately signed envelope for each read;
unsupported or temporarily unavailable read queries use serial checks. No
identity, ACL or configuration is cached. Permanent authority or privacy failures
stop before change-stream reads; a transient sibling cannot hide them. Archived
label history stays readable while a physically archived namespace is rejected.

## Listen and resume

Start each worker's independent fresh-tail listener before reading its known
initial task reference, so follow-up messages sent during setup are retained:

```sh
msg --agent bot2 listen --remote --from-now --once \
  --cursor-file ./remote-bot2.cursor.json
msg --agent bot2 listen --remote --cursor-file ./remote-bot2.cursor.json \
  > ./remote-bot2.events.jsonl &
```

Each matching event is one flushed JSON line; empty polls produce no stdout.
The queue is checked once per second by default. This is a blocking CLI backed
by polling, not SSE or WebSocket. Consume background JSONL output incrementally.

`--event subagent.message` filters events. `--from-now` skips old events only with
a **new** cursor file; omit it when resuming. `--once` drains available events
and exits; `--max-events 1` waits for one matching event and exits. For a task
runner waiting for one result, run this without `&`:

```sh
msg --agent bot2 listen --remote --cursor-file ./remote-bot2.cursor.json --max-events 1
```

Clients whose `msg listen --help` lists `--max-pages` can bound a single drain:

```sh
msg --agent bot2 listen --remote --once --max-pages 8 \
  --cursor-file ./remote-bot2.cursor.json
```

`N` must be a positive integer and requires `--once`. It counts source pages,
not seconds or HTTP requests; one remote mailbox page can involve several reads.
The client preserves its cursor, pending events and `has_more`. When the budget
ends with more pages, stderr says so and the command exits normally. Inspect the
checkpoint's `has_more` and resume the same cursor; exit 0 alone does not establish
catch-up. Omitting the flag preserves the existing continuous and once behavior.
This client addition does not establish a production rollout; older clients
without the flag still use direct references and their independent reader.

`--interval 0.5` changes the polling interval. Stop with Ctrl-C or SIGINT. A cursor
belongs to its server, account, mailbox and event filters. Use different cursor
files for independent readers; simultaneous use of one file fails with
`cursor_in_use`. Reading never implies ACK. A crash between output and checkpoint
persistence can replay an event: deduplicate by message `id`. Retry sends with
the same `agent send --message-id ID` and message content.

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
When the server provides current-parent hints and protected event resume cursors,
the client skips unrelated message-body reads and can fetch larger internal pages
without exceeding the requested message limit. A `tail_cursor` lets it start at
the end without reading old message bodies. Servers without these output hints
use the original scan. There is no server-side mailbox index, so a cold scan can
still require linear database work.

`listen --once` follows `has_more` until caught up, including pages of unrelated
activity. Even an existing cursor can take time to drain on a busy account. Read
known task/result Resource references directly and retain the independent
listener cursor for follow-up messages; do not restart from account history.

The current change stream scans at most 64 stored events per page and yields during
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
login, `--username alice` permits a purely local label namespace; this does not
register or reserve `alice` on a server. An older local identity may need this
explicit username; the client remembers it. Local listeners use the same flags
above with `--offline --username alice` and without `--remote`.

## Existing account events

```sh
msg listen --event communication.dm_send --cursor-file ./account.cursor.json
```

Without `--agent`, `msg listen` reads the account's existing authorized change
stream. It does not create a resource subscription or expand access. Continue to
use the existing watch commands for resources you want to subscribe to.

The CLI and JSONL format do not depend on particular agent software. An agent
that can run commands or read a file can participate. Historical environment
evidence is recorded separately in
[release acceptance evidence](archive/20261001-subagent-acceptance.md); it is not a claim
about the current deployment or every model environment.
