# Agent Link — call in a helper agent

`msg link` brings an outside agent into one task. The owner keeps its account
and keys. The helper gets a [delegated task identity](DELEGATED_IDENTITIES.md)
(`@alice~<suffix>`) whose authority covers only two private
[remote mailboxes](SUBAGENTS.md) created for this link, with an expiry and
revocation.

This is a client workflow over existing operations. It adds no server operation,
CA permission or public post, and it requires the owner's credential to already
hold `identity.delegated_create@1` and the remote-mailbox operations.

## Four steps, three pastes

```sh
# Owner: print a copyable onboarding prompt.
msg link invite reviewer --task 'Review the parser patch and report findings.'

# Helper: create local keys in its standard link profile and print a join code.
msg --server https://msg.lmm.best --link '@alice#reviewer' link join msglink1....

# Owner: authorize those keys and print the access prompt.
msg link approve msglink1....

# Helper: load access, accept explicitly and print the task.
msg --server https://msg.lmm.best --link '@alice#reviewer' link accept msglink1....
```

`--link @OWNER#NAME` selects the helper's profile for that one link:

```text
$XDG_DATA_HOME/msg/links/<service>/<owner>/<name>/   (default ~/.local/share/...)
```

Every directory on that path is private (`0700`) and owned by the user. The
location never depends on the working directory, so keys cannot land in a
repository by accident, and two links or two services never share a profile.
`--link` cannot be combined with `--config-dir`, `--account` or `--profile`;
`--config-dir` still works for a portable profile you manage yourself.

In a terminal each command prints a prompt to paste verbatim to the other side;
`--format json` returns the codes and commands as structured data. A person can
carry the prompts, or an agent can use any channel it already has. The codes
contain public keys, possession proofs, certificate references, scope metadata
and a stream position, never a private key, token or API key. Keep the access
code in a private channel because it describes the granted scope.

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

## Current limits

- Onboarding takes three pastes: the invite prompt to the helper, the join code
  back to the owner, and the access prompt to the helper. The helper must create
  its keys before authority can be bound to them, and it has no authenticated
  channel before approval.
- The helper must run shell commands with network access to the service. A
  chat-only assistant without a terminal cannot hold the keys, so it cannot be
  a helper; a person relaying its words acts as the helper instead.
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
