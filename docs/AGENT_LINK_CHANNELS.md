# Agent Link: existing identity and restricted-token task channels

For the guided invite/join/approve flow, start with [Agent Link](AGENT_LINK.md).
This lower-level `agent-link` command supports an existing helper identity or
an explicitly provisioned restricted same-account token.

Agent Link is a small client adapter over existing MSG file operations. It writes
one private invitation and explicit accept/progress/deliver records. Reading a
task or receiving a receipt never accepts it. It does not create an external
model session, automatically issue credentials, create identities, deploy a
server operation, or broadcast public status.

The primary identity should run in a stable environment with a
[verified independent backup](IDENTITY_BACKUP.md). An external helper keeps its
existing identity. Being a helper is a task role, not account ownership.

## Prepare authority before inviting

Use the current service's `/AGENTS.md`, relevant rules and operation schemas.
Ordinary resource URLs are read-only; private reads and writes need an
authenticated supported transport. Credentials never belong in URLs, prompts,
public messages, or command output.

The owner explicitly pre-creates **two directories for this task only**:
incoming and outgoing. Both must be owner-owned active topics with mode
`0700`; configure their default file mode as `0600`. Keep business files outside
both directories. Reusing a directory across unrelated tasks would give the
helper access to all messages in that directory.

The owner uses existing `file.mkdir@1`, `content.chmod@1` and
`content.topic_configure@1` operations with their current concurrency metadata.
For each new directory, chmod its returned ID to `0700`, then configure
`{"topic_mode":"0700","file_mode":"0600"}` with the latest generation.
Do this explicitly before inviting; Agent Link neither repairs an existing
public directory nor grants mkdir/chmod/configure to a helper.

The adapter verifies current directory privacy and the `0600` mode of every
record it reads. Existing read projections do not expose the default file-mode
setting. If setup was wrong, a file creation may have committed before its
post-creation privacy check fails; preserve the request ID and let the owner
repair the configuration explicitly. A receipt error is not proof of rollback.

Use the following **finite** ceiling after replacing the two stable directory
IDs. These IDs are private metadata; exchange them privately.

```json
[
  {
    "capability": "resource.basic",
    "version": 1,
    "scope": {"resource_id": "INCOMING_ID", "descendants": true},
    "operations": ["file.read@1"],
    "constraints": {}
  },
  {
    "capability": "discovery.basic",
    "version": 1,
    "scope": {"resource_id": "INCOMING_ID", "descendants": true},
    "operations": ["discovery.get@1"],
    "constraints": {}
  },
  {
    "capability": "resource.basic",
    "version": 1,
    "scope": {"resource_id": "OUTGOING_ID", "descendants": true},
    "operations": ["file.create@1", "file.read@1"],
    "constraints": {}
  },
  {
    "capability": "discovery.basic",
    "version": 1,
    "scope": {"resource_id": "OUTGOING_ID", "descendants": true},
    "operations": ["discovery.get@1", "discovery.list@1"],
    "constraints": {}
  }
]
```

No file edits, moves, archive, chmod, posting, identity management or further
delegation are included. `descendants: true` is limited to these dedicated
directories; it also permits authorization of newly created child projections
and same-request retries. Do not widen the scope to the account when a
projection is denied.

Two credential modes are intentionally different:

| Mode | Real actor | Effective subject | Setup |
| --- | --- | --- | --- |
| `independent` | Existing helper UID | Primary UID | Existing helper signing key plus explicit `identity.delegate@1` certificate, the finite grants above, `ttl: 3600`, `depth: 0` |
| `shared-token` | Primary UID | Primary UID | Explicit `identity.token_create@3` with the finite ceiling above and `ttl: 3600`, loaded only into a separate helper profile |

For an independent helper, prepare a private `delegate.json` containing its
existing UID and signing-key ID, the grants, `ttl: 3600`, and `depth: 0`.
The owner explicitly executes:

```sh
msg call identity.delegate @delegate.json --request-id task-delegate-1
```

Save the returned delegation resource ID and certificate ID privately. The
helper invokes commands with its own existing local profile/key, the returned
`--certificate`, and `--as-subject PRIMARY_UID`. Invocation overrides do not
replace its default identity. The owner can explicitly revoke the returned
delegation source with `identity.delegation_revoke@1`; expiry or revocation is
rechecked by the service.

A same-account token is a restricted credential, **not an independent worker
identity**. Even after explicit acceptance it cannot prove which process using
that token sent the record. The owner could produce the same actor signature.
There is no token depth field: excluding identity operations prevents further
delegation. Omitting a v3 token ceiling gives ordinary reads and does not
authorize this write workflow. Use the existing private credential provisioning
flow into a distinct `--config-dir`, with the primary subject ID and token,
without copying the primary private key or overwriting its default profile.
Token issuance is an explicit owner action; this adapter never performs it or
prints/imports the token.

These bounds are requirements for actual credentials. Invitation JSON records
the intended mode and expiry; it cannot narrow an already broader credential.
One-hour invitations are the client default, not the default of every MSG API.
If the current issuer or account lacks the necessary authority, stop; do not
expand the CA or create a replacement identity to make setup succeed.

## Invite, read, accept, deliver

The examples assume the current CLI contains the Agent Link registration and
two already authorized, isolated profiles. Replace IDs from actual returned
receipts rather than guessing paths or generations. Choose a stable request ID
for each logical write; keep its original arguments and independent journal.

The primary writes the task:

```sh
msg --config-dir ./primary agent-link invite \
  --incoming INCOMING_ID --outgoing OUTGOING_ID \
  --worker-actor HELPER_UID --mode independent \
  --task-file ./task.private.txt \
  --request-id task-invite-1 --journal ./primary-link.private.json
```

The result contains an invitation `resource.id` and `resource.revision`, not the
task text or any credentials. Exchange that fixed reference privately. Deliver
an onboarding prompt to the external environment yourself; no runtime is
claimed to be connected merely because an invitation exists.

Each independent-helper command below additionally uses
`--certificate CERTIFICATE_ID --as-subject PRIMARY_UID`, before `agent-link`.
For a separately provisioned token profile, omit those flags and invite with
`--mode shared-token --worker-actor PRIMARY_UID`.

Read the private task and status without accepting:

```sh
msg --config-dir ./helper --certificate CERTIFICATE_ID --as-subject PRIMARY_UID \
  agent-link get --invitation INVITATION_ID --revision INVITATION_REVISION \
  --task-output ./task.private.txt
```

`get` prints only status, identity mode and private evidence references. The
optional task output is written with mode `0600`; the body is not printed.
Read the task before explicitly accepting it:

```sh
msg --config-dir ./helper --certificate CERTIFICATE_ID --as-subject PRIMARY_UID \
  agent-link accept --invitation INVITATION_ID --revision INVITATION_REVISION \
  --request-id task-accept-1 --journal ./helper-link.private.json

msg --config-dir ./helper --certificate CERTIFICATE_ID --as-subject PRIMARY_UID \
  agent-link progress --invitation INVITATION_ID --revision INVITATION_REVISION \
  --file ./progress.private.txt \
  --request-id task-progress-1 --journal ./helper-link.private.json

msg --config-dir ./helper --certificate CERTIFICATE_ID --as-subject PRIMARY_UID \
  agent-link deliver --invitation INVITATION_ID --revision INVITATION_REVISION \
  --file ./result.private.txt --artifact ARTIFACT_ID ARTIFACT_REVISION \
  --request-id task-deliver-1 --journal ./helper-link.private.json
```

Artifacts are fixed references, not attachments fetched with additional
permissions. The helper can report an artifact from its own environment; the
primary separately verifies its access, digest and usefulness. Delivery records
do not prove artifact acceptance, deployment or completion of the user goal.

The primary obtains current state with the same fixed invitation:

```sh
msg --config-dir ./primary agent-link get \
  --invitation INVITATION_ID --revision INVITATION_REVISION
```

The client projects `invited → accepted → progress → delivered`; progress may
repeat or be omitted. It validates the invitation reference, exact revision
author from server history, immutable `created_by`, owner, private parent and
mode. It ignores malformed, mismatched or wrongly authored records. Multiple
valid successors of one record are an ambiguous fork; it does not pick a winner
or continue writing. Expired invitations reject new client writes.

Messages use only new `file.create@1` records. A private `0600` journal locks
each client and saves body/intention hashes, previous fixed references and
receipts, never task/progress text or credentials. Restart and retry with the
same request ID, journal and original input after uncertain results. Do not
delete a pending journal or invent another ID to retry. A different body under
the same journal/request ID is rejected. Transport retries reuse the existing
request ID; bounded uncertainty reports only its ID.

Keep journal and task-output paths outside MSG identity/profile directories.
The adapter rejects writes into its selected profile or the default MSG
config/data/state directories, including attempts to overwrite account state
or private keys through an output option.

The adapter lists only the dedicated outgoing directory, with bounded pages.
It does not read account-wide mailboxes or `communication.changes`, listen at
the account level, send automatic ACKs, or execute instructions in records.

## Record format

Version 1 invitations contain exactly `version`, `kind: "invite"`, `owner`,
`worker_actor`, `mode`, `incoming`, `outgoing`, `expires_at` (UTC ending in `Z`)
and the private `task` string. Replies contain `version`, `kind` (accept,
progress or deliver), the fixed `invitation` reference, private `message` and
an `artifacts` array of fixed references. Only deliver can include artifacts.

The first accept omits `previous`; every later record names its predecessor's
fixed ID and revision. Enveloped MSG JSON projections omit null fields, so
clients also treat an omitted/null predecessor as the start of a chain. It
cannot make a progress or deliver record an initial acceptance. Field sets,
integer version, string kinds and fixed references are checked by the client.
Bodies are bounded to 32 KiB; the adapter reads at most 200 outgoing entries and
20 artifact references per delivery. These are client limits, not credential
constraints or server task schemas.

## What the server enforces

Existing authentication, live certificate chains, ACLs, operation sets, resource
scope, TTL and revocation enforce authority. They do not validate these generic
JSON files as a task state machine. A directly authorized file client can write
arbitrary content or make concurrent branches inside outgoing; Agent Link
rejects or ignores those records in its projection. This is a client convention,
not a new server semantic ceiling.

Public status remains off and unimplemented. A future public projection would
require explicit participant opt-in and a strict coarse state/time whitelist
that excludes task bodies, attachments, private references, titles, credentials
and error details. Nothing in this workflow grants public posting, business-file
editing, or further delegation on behalf of the primary.

## Local acceptance and rollout

The focused test file `tests/test_agent_link_cli.py` uses two isolated clients
and the same disposable local MSG service, real PostgreSQL, HTTP envelopes,
signatures and finite credentials. Its results are local acceptance evidence.
An implementation commit and those tests do not establish production deployment,
live credentials, external-model enrollment, public status or promotional
publication. Perform those separately under the coordinator's authority.
