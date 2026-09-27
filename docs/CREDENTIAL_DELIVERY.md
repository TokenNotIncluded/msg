# One-use credential delivery and response loss

Credential issuance commits a non-secret business result before attempting to
release a token. An atomic PostgreSQL claim is consumed before a transport can
send that token. Sending, receiving, or acknowledging an HTTP response is not
proof that the client saved the credential.

## Supported contracts

`SECRET_DELIVERY_MIN_VERSION` in `core/requests.py` is the single policy used by
the executor, release hook, batch exclusion and URL-packet boundary:

| Operation | Minimum version | Client intention |
| --- | --- | --- |
| `identity.temporary` | 3 | `temporary.json`, with pre-saved independent Ed25519/age keys |
| `identity.custodial_create` | 2 | `custodial-bootstrap.json` |
| `identity.token_rotate` | 2 | `token-rotation.json` |
| `identity.token_create` | 2 | Caller saves the exact signed request and independent recovery material before sending |
| `identity.token_recover` | 1 | Successor intention saved before submitting recovery |

HTTP, GraphQL and MCP use request bodies. No batch may issue a credential.
PathGET rejects these operations and TokenProof with `secure_channel_required`,
including hand-built JSON/gzip packets. A caller unable to provide a secure
channel can still use public and secret-free GET reads. The client convenience
methods retain their existing `token_secret_transport_required` and
`token_secret_tls_required` local error codes; neither falls back to a URL.

## State transitions

1. Save the keys, random issuance nonce, independent recovery secret, fixed
   request ID and intention before sending. The recovery secret is not the
   issuance nonce. The server stores only verifiers and signed non-secret results.
2. Commit credential, recovery verifier/deadline, Event and operation result in
   one transaction. A rollback leaves no committed issuance.
3. Atomically claim release. Concurrent replies cannot release twice. On replay,
   return `token_delivery_unavailable` with `data.committed_result`: the original
   non-secret OperationResult, including its timestamp and verifiable receipt.
   Decode that nested result and use the ordinary `receipt_bytes` verification;
   the outer delivery error is not a new signed business result.
4. When release or its response was lost, submit `identity.token_recover` with
   the credential ID, original request ID and **pre-bound recovery secret**.
   Save a new nonce, successor request ID and independent `new_recovery_secret`
   before sending. The IDs locate the lineage; none authorizes recovery alone.
5. Recovery atomically consumes that predecessor's recovery authorization,
   revokes it and creates one successor with the same ceiling and token expiry.
   Each successor also inherits the original recovery deadline. The default
   window is 15 minutes from the original business commit, configurable from
   1 to 60 minutes and capped by credential expiry.
6. A second lost recovery response is resumed with the same request ID and input.
   A release-unavailable response advances the local intention to the previously
   saved successor material. Another explicit recovery then replaces that
   successor; it never resurrects a predecessor or extends the lineage deadline.

Short request expiry is separate from both credential expiry and recovery
expiry. A restarted client generates a fresh bounded request expiry and signs
again where applicable, preserving the business request ID and input digest.
It must not extend the token lifetime or the recovery window. Expired recovery,
revocation, missing material, mismatched subject or a conflicting request fails
closed. Expired windows require a separately authorized RecoveryPolicy; knowing
an ID, old bootstrap nonce or old token is not a substitute.

## Local durability

Built-in token operations share a nonblocking OS lock with ordinary identity
upgrades. They reload accepted local state under the lock, so a stale client
cannot overwrite another process's successful upgrade or create a second
account. Journals are owner-only regular files with one hardlink, no symlink and
an 8 KiB bound. New intentions bind server and temporary key pair. Missing keys
are not regenerated. Older journals can use their existing pre-bound recovery
material but cannot reconstruct a missing key or manufacture a server binding;
keep them with the original protected client state and server configuration.

The client saves and fsyncs its accepted identity/token before deleting and
fsyncing the journal. Recovery checks **both** the predecessor and pending
successor IDs against local accepted state. A crash after saving a successor
but before removing its journal is local cleanup only: no network call and no
second rotation. An unsafe or inconsistent journal remains in place for explicit
resolution, not deletion followed by a new bootstrap.

Resume a built-in pending intention with `msg identity recover-token`.
`msg identity temporary` and `msg identity rotate-token` retry their fixed
original intentions before recovery is needed. For raw `token_create`, save
its signed packet and recovery material in a protected local file before
`transport.call`; use `identity.token_recover` with its saved lineage on loss.
Raw API calls do not implicitly journal on the caller's filesystem.

## Evidence and limits

`tests/test_credential_delivery_matrix.py` covers real HTTP/GraphQL/MCP response
loss twice, client/server restart beyond the old short proof expiry, local-save
crashes, missing or substituted keys, unsafe journals and concurrent clients.
`tests/test_credential_delivery_contract.py` covers every issuer's durable
pre-release commit, one-winner concurrent release, repeated recovery, unchanged
ceiling/expiry/deadline, both batch modes, both PathGET encodings, retained receipt
verification and absence of plaintext secrets in persisted results/events and
captured application logs. Existing recovery-window, token-release, URL-ingress
and negative-authorization tests remain required.

Bootstrap feature `credential_delivery` maps to a real read-only schema/policy
check and the isolated `credential_delivery_recovery` selftest. No new operation,
capability or database schema is introduced, and no old CA authorization expands.
Result retention currently has no independent GC policy; recovery eligibility
is nevertheless bounded by deadline and current authority. This does not certify
production reverse-proxy/APM logs, online backup retirement, production deployment
or custodial historical ciphertext migration; those have separate acceptance
issues and must not be inferred from a green source test suite.
