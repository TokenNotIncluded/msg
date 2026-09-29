# Credential delivery boundary progress

Issue #162; PR #172, stacked on #167 base2d1cc443c828dd2f735995cc6a5d20021fee6ac7 / tree6171f6041ffd0addaf72eadd35acf562f478281f. Branchfix/token-delivery-boundary-20260929. Coordination #83 comment5879008128. No other source branch is overwritten.

## Single source and transaction timing

security.token_delivery owns TokenDelivery and the one recovery_verifier function. Application constructs it, retains compatibility forwarding only, and binds HTTP/SSH executors directly to the same release method. Authentication consumes that verifier, not a second formula. Dependencies: metadata, the existing token derivation key, clock and current-window providers. No Root signer, vault, Registry, plugin or HTTP object.

The original issued_token/release/verifier bodies are unchanged; record_token_delivery changes only current-window access. Live clock/settings changes are preserved. The handler still records recovery binding inside issuance. After commit, release independently commits the one-time claim before exposing the response token. Lost responses still require recovery. IDs, operation versions, purpose strings, deadlines, persistent results/events/receipts retain their existing meaning. No project code/test executed locally.

## Independently verified cloud evidence

RED b68526267a4aaea2df8418ad4d400f64a845fa63 / tree0f653206afd064f3e752cbb3442c202fbd72a4bb, run36486490339, artifact10999622677: **7 failed /115 passed /0 errors /0 skipped**. All seven new ownership checks failed; all existing suites passed. ZIP SHA2560fed854c2890019a5892fbabdde7a6fe3093b05d6805b64d701504f9f44de958; source.json confirms head/tree/attempt1/Python3.15.0rc2.

First implementation f015c60f05469f0058255bd645afef084e87840d / tree46af80b065a8e4eaea319b87a70bb5ddb281f966, push run36486854337, artifact10998634353: **1 failed /121 passed /0 errors /0 skipped**. ZIP SHA2560186be18b2ed3393af3fe5eaa5ee11eb1caa315f941cd0a1ddd469fb39fdc189 and source.json independently verified.

The sole failure is a new test's SQL query for events.request_id, which does not exist: events stores seq/id/body and canonical Event JSON includes request_id. All earlier assertions in that case (concurrent one-time release, receipt verification, secret-free persistent result, recovery binding) passed. The corrected test reads real body JSON, still requires exactly one matching event, and additionally checks its subject and absence of both token and recovery secret. No production schema/code or old assertion is changed to accommodate a fabricated column.

The corrected head must rerun the full credential selection and full four-shard/node-ID/conformance/build/recovery gates. Neither RED nor first implementation is a passing merge candidate. Latest final status belongs to #172 comments/actual Actions, not historical evidence.

## Boundaries

#67 remains completed with every original token/credential/recovery-window/client/batch/security regression retained. This is ownership repair, not a new token protocol. No deployment, actual credentials, Root/PIN, funds, external delivery, backup destruction or promotion. #167 finishes its own stable combined-main gate separately.
