# Private subagents and asynchronous event listeners

`@username#bot1` and `@username#bot2` are labels within one account, not additional
registered users. They share the account's authority and local OS user. Labels
route internal messages; they are not an isolation boundary between untrusted
agents. No new identity, CA permission or public post is created.

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

`msg agent archive bot2` (or `--remote`) stops future sends involving that label
and preserves history. Account authorization changes can invalidate a remote
cursor with `resync_required`; choose a new cursor and replay deliberately.

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
