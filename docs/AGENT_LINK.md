# Agent Link — call in a helper agent

`msg link` brings an outside agent into one task. The owner keeps its account
and keys. The helper gets a [delegated task identity](DELEGATED_IDENTITIES.md)
(`@alice~<suffix>`) whose authority covers only two private
[remote mailboxes](SUBAGENTS.md) created for this link, with an expiry and
revocation.

The owner's credential must already hold `identity.delegated_create@1` and the
remote-mailbox operations. The claim rendezvous
(`identity.link_open`, `identity.link_claim`, `identity.link_release`,
`identity.link_collect`) does not add a CA permission or a public post. The
claim event is visible only on the owner's private change stream.

## One paste, then the agents talk

```sh
# Owner: print the only prompt a person has to carry.
msg link invite reviewer --task 'Review the parser patch and report findings.'

# Owner: approve the claim when it appears on this account's listener.
msg link watch

# Helper: create local keys, submit them to the invitation, and wait for access.
msg --server https://msg.lmm.best --link '@alice#reviewer' link join msglink1....
```

`link watch` reads `identity.link_claim` events from the owner's private change
stream and issues the delegated identity. The grant is sealed to the helper's
encryption key. `link join` collects it with the same possession proof, accepts
the task, and prints it. The person does not carry a join code or an access code.

If the claim cannot reach the service, `link join --no-wait` still prints a join
code and `link approve` / `link accept` carry it by hand.

`--link @OWNER#NAME` selects the helper's profile for that one link:

```text
$XDG_DATA_HOME/msg/links/<service>/<owner>/<name>/   (default ~/.local/share/...)
```

Every directory on that path is private (`0700`) and owned by the user. The
location never depends on the working directory, so keys cannot land in a
repository by accident, and two links or two services never share a profile.
`--link` cannot be combined with `--config-dir`, `--account` or `--profile`;
`--config-dir` still works for a portable profile you manage yourself.

In a terminal `link invite` prints the one prompt to paste to the helper.
`--format json` returns the codes and commands as structured data. The manual
join and access codes contain public keys, possession proofs, certificate
references, scope metadata and a stream position, never a private key, token
or API key. Keep a hand-carried access code in a private channel because it
describes the granted scope. The direct path seals that grant to the helper.

The helper's private signing and age keys are generated in its link profile
and never leave it. Every step is safe to rerun: `join` reuses the
same keys; `approve` replays a lost issuance response with the same request ID
and reprints the same access; `accept` reuses the same acceptance message ID.

## Working together

| Side | Mailbox | Command |
| --- | --- | --- |
| Helper reads tasks | `@alice#reviewer` | `msg --link '@alice#reviewer' --agent reviewer listen --remote` |
| Helper reports | `@alice#reviewer-lead` | `msg --link '@alice#reviewer' --agent reviewer agent send '@alice#reviewer-lead' 'result' --remote` |
| Owner reads reports | `@alice#reviewer-lead` | `msg --agent reviewer-lead listen --remote` |
| Owner follows up | `@alice#reviewer` | `msg --agent reviewer-lead agent send '@alice#reviewer' 'next' --remote` |

`approve` records tail positions of both mailboxes before the first link
message exists, and seeds each side's default listener checkpoint with it. Both
listeners therefore start at the task with no gap and never scan the account's
older history. An existing checkpoint is never overwritten. `accept` reads the
task by its known message reference. Reading never implies ACK, and listener
delivery is at least once: deduplicate by message `id`.

## Exact scope

The helper's ceiling, resolved to stable resource IDs at approval:

| Capability | Operations | Resource |
| --- | --- | --- |
| `discovery.basic` | `discovery.get@1` | `/@alice`, `/@alice/files`, `/@alice/files/agents` (that resource only) |
| `discovery.basic` | `discovery.get@1` | both link mailboxes and their contents |
| `resource.basic` | `file.read@1` | both link mailboxes and their contents |
| `resource.basic` | `file.create@1` | the `-lead` mailbox only |
| `communication.basic` | `communication.changes@1` | `/@alice` (the stream), both link mailboxes (its events) |

The helper cannot list sibling mailboxes, read another label, write into its
own task inbox, post, change permissions or create further identities. Change
events outside the two mailboxes are filtered by the same ceiling before they
are returned. Tests verify each of these denials returns `credential_ceiling`.
The account change stream can still show the helper events by the account that
carry no resource reference, as it does for any principal reading that stream.

## Status and revocation

```sh
msg link list             # local link records and workflow status
msg link status reviewer  # live delegated identity status from the server
msg link revoke reviewer  # revoke the identity and archive both mailboxes
```

Revocation is idempotent. It stops new access immediately
(`authority_source_inactive`); it does not undo committed writes or stop the
helper's process. Archived mailboxes keep their history, so a link name is not
reused: invite a new helper under a new name. Join and access codes are bound
to their invitation ID; an approval cannot be replayed after revocation.

## Existing accounts with an old permission ceiling

A primary signing key registered before Agent Link may return `credential_ceiling`
on `link invite`. Token rotation and certificate renewal do not add permissions.
An operator can preview the exact repair locally:

```sh
msgd account repair-link @alice --key-id CURRENT_PRIMARY_KEY_ID
```

To apply, run the same command with `--apply`. It shows the account and exact
permission additions; enter `y`, then the Root PIN in a private terminal
(`--allow-ssh` explicitly permits an OS-root SSH terminal). No digest needs to be
retyped. `--expected-digest` remains optional for pinning a prior preview; the
transaction always rejects changes since the displayed preview. The signed audit
records seven fixed owner operations; existing scopes, constraints, certificates,
secondary keys and tokens stay unchanged. Revoked, expired, delegated and
non-primary keys cannot be repaired. Repeating a completed repair is a no-op.
This command never extends CA issuance authority or grants a helper access.

## Current limits

- Onboarding takes one paste: the invite prompt. The helper claims that
  invitation with a new key, the owner sees the claim on their private listener,
  and the helper collects the sealed grant with the same proof. A person carries
  a join code and an access code only when `link join --no-wait` is used.
- The helper needs a terminal and either network access to the service or a
  preinstalled client plus a configured MSG MCP connector as described below.
  A chat-only assistant cannot hold the keys; a person relaying its words acts
  as the helper instead.
- The guided `msg link` flow uses a fresh helper link profile. To keep an
  existing MSG identity, use the explicitly configured
  [`agent-link` task-channel flow](AGENT_LINK_CHANNELS.md) with a finite existing
  identity delegation; it does not replace that identity.
- Lifetime is 1–1440 minutes and cannot exceed the owner's credential or the
  online CA limits.
- There is no public status feed for links yet.

Keep the primary and helper identity material in protected storage with a
[verified independent encrypted backup](IDENTITY_BACKUP.md). The task workflow
does not make a disposable agent environment durable.

For an explicitly chosen six-digit Root PIN, an OS-root private SSH terminal can
run `msgd root change-pin --allow-ssh --allow-short-pin`. Enter the current PIN,
then the new PIN twice. No typed confirmation or PIN in command arguments is
needed. The Root key stays the same; other key-wrapping flows retain their
normal passphrase requirements.

Interactive administration exits cleanly on Ctrl+C, end-of-input, or a declined
confirmation: `status: cancelled`, exit code 130, with no traceback. Cancelling
at any PIN prompt leaves the encrypted Root envelope unchanged.

## Recoverable join and private connector exchange

`link join --timeout 300` bounds the authorization wait in seconds. A timeout
keeps the same keys and invitation; repeat the same command to resume. Repeating
an accepted join rechecks expiry and live authority before returning the task.
An interrupted acceptance resumes its saved public grant and reuses its message
ID. Watchers sharing an owner profile serialize approval so mailbox setup and
issuance cannot race. Stage messages go to stderr and never claim that submission implies reading.
Successful text and JSON results include complete `commands.send` and
`commands.listen`, including the server, link, agent and selected transport.

Normal clients submit signed operations with POST. Exa is for public
instructions; do not use crawlers as private request relays or relax GET guards.

For a locally installed client whose terminal cannot access the network, an
agent with a configured MSG MCP connector can select `--transport tool_bridge`.
Run the command in a background terminal session. The client prints its private
`tool-exchange` directory beneath the helper link profile. For each pending
`*.request.json`, the agent runtime:

1. Verifies that `connector` is the configured MSG service's `/-/mcp` endpoint.
2. Calls the named MSG MCP tool with the exact `arguments.packet` object.
3. Reads the tool's full `structuredContent` OperationResult; never a crawler
   excerpt, cached webpage or a truncated text summary.
4. Atomically writes the indicated response file (mode 0600) as
   `{"exchange_id":"...","server":"...","result":{...}}`, preserving the
   original exchange/server identifiers and complete result including errors.

The client validates service, request ID and operation. Keys remain local;
reusable token credentials and secret-delivery operations are disallowed on
this bridge. Timeouts preserve pending files for retries. Tool calls are finite;
use `listen --remote --once --max-pages 1` repeatedly for follow-ups, preserving
its cursor. No human carries join or access codes.

This adapter does not install a client into an offline terminal, register a
ChatGPT connector, or prove that ChatGPT currently has that connector. It needs
a preinstalled/offline-installed client and an actual MSG request tool. Exa
alone cannot satisfy those prerequisites.

Ingress diagnostics attach `X-Msg-Trace-ID` and write only stage, registered
operation name, status, stable error code, elapsed milliseconds and trace ID to
the service journal. They also cover outer GET rejections. Ordinary URL/body
access logging stays disabled; do not enable it to debug signed requests.
