# Credential delivery and recovery

This protocol separates a committed credential from releasing its plaintext.
The server does not promise exactly-once network delivery. See issue #67; legacy
temporary identity **upgrade**, which does not issue a token, is documented in
[TEMPORARY_UPGRADE.md](TEMPORARY_UPGRADE.md).

## One policy, every entry

`msg.core.requests.SECRET_DELIVERY_MIN_VERSION` is the authoritative list:
`identity.temporary@3`, `identity.custodial_create@2`,
`identity.token_create@2`, `identity.token_rotate@2`, and
`identity.token_recover@1`. The executor, receipt encoding, delivery hook,
batch rejection, and URL/client guards consume that list. Earlier issuance
versions cannot execute or replay a historical plaintext credential.

HTTP POST, GraphQL and MCP HTTP carry the same envelope. The official client
requires HTTPS, except explicit loopback/test-server development origins.
PathGET refuses the envelope before building/sending a credential-bearing URL;
atomic and independent batches refuse secret-issuing children. An incapable
transport reports `secure_channel_required`; it never falls back to a URL,
older operation version, or another origin. This code does not certify external
proxy/CDN/APM logging: deployment verification remains a separate requirement.

## Commit, claim, recover

Before the first request, the client durably saves a random nonce, an independent
32-byte recovery secret, the fixed business request ID, original inputs, and
expected subject/credential IDs. A temporary identity also saves its independent
Ed25519 and age keys first. These are owned local files, mode 0600 in a 0700
directory. The server stores the recovery verifier and binding, not the plaintext
secret or token. A PostgreSQL transaction commits the credential and its signed,
non-secret operation result.

The response hook then atomically claims delivery in a separate transaction.
Only the winner obtains plaintext. A repeat of an already claimed request returns
`token_delivery_unavailable` with `data.committed_result`: the **unchanged signed
business result**, including its subject, request ID, credential ID, expiry and
original receipt. The error itself is not passed off as that signed success.
`receipt_bytes` excludes the response-only token. Neither replay nor inspection
of this result grants authority to other operations.

If the successful response is lost, `identity.token_recover` must prove the
independently pre-bound recovery material. In one transaction it consumes that
material, revokes the predecessor, and creates a successor with the predecessor's
**current ceiling, exact expiry and original recovery deadline**. A revoked or
expired predecessor cannot be revived. Concurrent claims cannot release a second
plaintext token or create two usable successors from the same recovery material.

The recovery window defaults to 15 minutes. The existing bounded configuration
permits 1–60 minutes, and the deadline is also capped by the original credential
expiry. Time starts at the original issuance commit, not at response delivery or
a later retry. At the deadline, recovery fails closed. Non-secret results and
claim/consumption tombstones remain durable with the installation's idempotency
history; this change adds no TTL deletion or read-triggered cleanup. Retaining a
record never makes expired recovery material valid again.

## Recovering a lost recovery response

Before submitting recovery, the client first saves the successor's request ID,
nonce and independent **next** recovery secret within the existing journal.
After an uncertain response it retries that same business request. If the server
reports that its release was already claimed, the client checks the returned
non-secret subject, request ID, predecessor and successor binding, then durably
advances to the already committed successor. A further explicit recovery uses the
successor's pre-saved material; it does not resend a previously released token.

A request proof normally expires after three minutes, independently of the
recovery window. A restart refreshes only this short request expiry/proof. The
business request ID, original inputs and payload digest stay unchanged, so this
cannot extend a credential or its recovery deadline. Clients must keep their
clocks reasonably synchronized; proof renewal is not server-clock override.

One OS lock coordinates token issuance, rotation/recovery and temporary upgrade
for a client directory. The lock is released on process exit. Existing client
state is read without rewriting it. The accepted credential is durably saved
**before** the journal is removed and its directory synchronized. A restart after
that local save recognizes the accepted successor and removes only the completed
journal, without rotating/revoking that already saved credential.

## Official client

Use `msg identity temporary`, `msg identity rotate-token`, and
`msg identity recover-token`; `MsgClient.custodial(handle)` uses the same journal.
For signed issuance, `msg identity create-token --ceiling @grants.json --ttl 900`
uses `MsgClient.create_token(ceiling=[...], ttl=900)`. The ceiling file is a JSON
array of existing registry grants, not permission expressions. The new token is
saved as `credential-<credential_id>.json` in the private client directory and does
not replace the caller's signing identity. Successful issuance still returns its
one-time result on stdout; treat redirected output as a secret.

`msg identity show` reports only the pending operation, request ID, status and
resume command; it never prints a journal's nonce or recovery material. Identity
mutation helpers operate on the local primary identity, not invocation-only
`--key`, `--certificate` or `--as-subject` overrides. Generic signed operations
retain their existing override support.

After a failure **before submission**, retry the original issuance command with
the same inputs. After `token_delivery_unavailable`, use `recover-token`. If that
recovery response was also lost, the first recovery retry can report
`token_delivery_unavailable` while advancing the local journal; the next explicit
`recover-token` obtains a successor within the original window. No automatic
loop creates fresh identities or renews a deadline.

Compatible old journals preserve their original IDs/material, including earlier
bootstrap descendants whose journal did not record a generation. Unsupported
legacy contracts, corrupt/foreign journals, missing keys, symlinks, hardlinks,
FIFOs, public file permissions and multiple pending token journals fail closed.
Do not delete a journal to work around uncertainty. Without the bound material,
or after its deadline, use a separately authorized RecoveryPolicy if configured;
a nonce, request ID, old revoked token, or a receipt alone is not account recovery.

## Verification

`tests/test_credential_recovery_matrix.py` exercises the real PostgreSQL-backed
application through HTTP, GraphQL and MCP HTTP: precommit loss, loss before and
after claim, repeated recovery loss, refreshed proofs, server/client restarts,
concurrent release, original-deadline expiry, narrowed ceilings, current
revocation, and crashes between local save and journal deletion. It also checks
PathGET, batch and plaintext-HTTP refusal before dispatch.

`tests/test_credential_journal_safety.py` checks owned-file boundaries, malformed
and legacy journals, independent material, missing-key refusal, non-secret status,
and cross-process locking. Existing token-delivery/window, upgrade, URL-security
and ingress-log suites remain required. Production logging and restore drills are
not inferred from these isolated tests.
