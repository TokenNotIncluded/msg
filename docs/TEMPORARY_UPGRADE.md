# Recovering a temporary identity upgrade

`msg identity upgrade HANDLE` upgrades an existing temporary subject without
changing its subject ID, resource ownership, memberships or signed history.
It uses the currently valid temporary credential and proof of possession of
the new independent Ed25519 identity key and age encryption subkey. The server
atomically binds the keys, issues the authorized base certificate, commits the
upgrade result and revokes the temporary tokens. None of these steps can be
recovered by knowing an account name, request ID or old bootstrap nonce alone.

## Before sending

The client first saves its identity key and encryption subkey in its protected
client directory, then durably writes `identity-upgrade.json` with mode 0600.
The intention contains the fixed request ID, server, existing subject, requested
handle, public keys and original credential **ID**, not a token or private key.
An OS file lock excludes simultaneous upgrades using the same directory without
blocking the event loop. A crash releases the lock; a lock-file path by itself
is not evidence that another process is running. Keys and pending intentions
must be retained together. Symlinked, overlarge, mismatched or insecure journal
files fail closed and are never used to regenerate or replace a key.

The secret-bearing upgrade uses HTTPS request bodies through HTTP, GraphQL or
MCP. Local test/loopback exceptions are the existing client policy. PathGET
cannot be selected for this token-bearing submission; it is rejected before
keys or network requests are created. This does not disable public GET or
signed, secret-free status reads.

## Committed, but no response received

`identity.upgrade_result` is a normal registered **read** operation. Its only
argument is `upgrade_request_id`. The client sends a fresh short-lived request
signed by the new identity key, without the revoked temporary token, a bootstrap
claim or a cached certificate. All transports use the same authentication and
current authorization pipeline. The request ID only locates a result belonging
to the authenticated subject.

The handler requires a successful stored `identity.upgrade` result, the exact
new primary identity key, its current credential ceiling, the same active
subject-owned encryption subkey, and the still-valid certificate and issuer
chain. A second key, another subject, a revoked key/certificate or an expired
request cannot use the result to acquire authority. The response is a small
whitelist: subject/key/encryption-key identifiers, encryption recipient,
certificate ID, handle snapshot, original commit time, original request ID and
`status=completed`. It never delivers an old token, issues a new certificate,
changes identity or appends an Event/result. Signed PathGET is available for
this non-secret read.

The client validates the result against its saved intention, durably switches
its local identity and removes its old token before deleting and fsyncing the
journal. A crash between the local switch and journal removal is safe to resume.
A lost recovery response leaves the same journal and keys in place. Restarting
both client and server, or expiration of the old token after the upgrade
committed, does not require that revoked credential to become valid again.

## Restart and failure handling

Run `msg identity show` to see a minimal `pending_upgrade` status and resume
hint; it does not print the journal's key material or credentials. Resume with
`msg identity upgrade` (no handle is required for a saved intention). The client
first asks for the signed result. If the server had not yet committed, only the
original **still-valid** temporary credential can resubmit the same intention
and request ID. Refreshing a request's short proof expiry does not renew the
credential or change the signed upgrade input. Conflicting concurrent upgrades
can have only one winner; a losing client cannot claim the winner's identity
key or create another subject.

Absent keys, a changed target/intention, expired credentials before commit,
revoked current authority, a server-side rollback, or an unavailable status
response remain explicit failures/pending states. Do not delete a pending
journal to force a new attempt. A pre-commit rollback leaves the old temporary
credential valid according to its original expiry. Where current authority is
no longer available, use an explicitly authorized recovery procedure; the
client never treats the saved request ID as a recovery capability.

Stored operation results currently have no independent expiry/garbage-collection
policy. Recovery therefore uses the existing retained result and remains
bounded by **current** key/certificate authority, not an everlasting secret
URL. A future retention policy must preserve this lookup contract or explicitly
report `upgrade_not_completed`; it must not fall back to ID-only authorization.
The certificate itself can expire or be revoked, in which case this endpoint
fails rather than reissuing it as a side effect of a read.

## Deployment and verification

The new read is explicitly included in the finite `identity.basic` operation
family. Adding it to source does not expand an old signed CA/credential snapshot.
`msgd doctor` reports missing signed authority through `authority_snapshot`;
any necessary CA update still requires the existing explicit local approval
procedure. There is no automatic repair or privilege increase on startup.

Bootstrap feature `identity_upgrade` maps to the actual read-only authority
check and isolated `identity_upgrade_recovery` selftest. The selftest performs
real temporary registration, upgrade, new-key-only lookup and rejection of the
revoked token on its isolated Test Root installation. It does not use production
accounts. Transport/crash tests separately discard actual committed HTTP,
GraphQL and MCP responses, then restart client and server and compare retained
business facts. Run:

```sh
python3.15 -m pytest tests/test_upgrade_journal.py \
  tests/test_temporary_legacy_migration.py tests/test_client.py \
  tests/test_feature_manifest.py tests/test_operations_admin.py
```

The tests also cover current-key/certificate revocation, repeated status reads,
pre-send and post-local-save crashes, transactional rollback, the same-directory
OS lock, competing upgrades, expired old tokens, PathGET equivalence and unsafe
journals. These tests do not certify unrelated custodial history migration or
production backup retirement.
