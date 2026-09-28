# Credential delivery boundary progress

Issue #162, supplemental to #167; base2d1cc443c828dd2f735995cc6a5d20021fee6ac7 / tree6171f6041ffd0addaf72eadd35acf562f478281f. Branchfix/token-delivery-boundary-20260929. Coordination #83 comment5879008128. No other source branch is overwritten.

## Source ownership

Before: Application owns composition plus issued_token/recovery_verifier/record_token_delivery/_secrets_for_caller. Authentication contains a second recovery-verifier formula.

After: security.token_delivery owns TokenDelivery and the one recovery_verifier function. Application constructs it, retains compatibility forwarding only, and binds all executors directly to the same release method. Authentication consumes the same pure verifier. Domain dependencies are explicitly metadata, the existing token derivation key, clock and a current-window provider; there is no Root signer, vault, Registry, plugin or HTTP object.

Current providers deliberately preserve later settings/clock changes. Static text/AST review found the issued_token and release bodies identical to the original, recovery_verifier body identical, and record_token_delivery identical except access to the narrow current-window provider. No project code/test was executed locally.

## Timing and compatibility

The existing handler records recovery binding inside its issuance transaction. After it commits, release opens its own write transaction and commits the one-time claim before adding the token to response data. A failed/dropped response still requires the existing independent recovery operation. Results, events, original receipts, IDs, purpose strings, deadlines and operation versions retain their meaning. No new generic response hook, protocol version, identity or recovery mechanism.

## Regression and cloud evidence

Tests-first headb68526267a4aaea2df8418ad4d400f64a845fa63 / tree0f653206afd064f3e752cbb3442c202fbd72a4bb contains seven new ownership/construction/wire/concurrent-claim/expired-refusal cases. Focused cloud workflow retains all existing token, credential, recovery-window, client recovery, batch secret and security regressions. The red focused run is not cancelled by the later implementation head; both are bound to exact source identity. Results remain pending until actual artifacts are read.

Final focused/full four-shard/node-ID/conformance/build/recovery outcomes are pending. No deployment, actual credentials, Root/PIN, backup destruction, external delivery or recovery promotion. #67 stays completed and its original functional regressions remain binding; this change is ownership-only, not a new token protocol. #167 must finish its own combined main gate separately.
