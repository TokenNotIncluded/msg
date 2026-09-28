# Credential delivery boundary progress

Issue #162, supplemental to #167; base2d1cc443c828dd2f735995cc6a5d20021fee6ac7 / tree6171f6041ffd0addaf72eadd35acf562f478281f. Branchfix/token-delivery-boundary-20260929. Coordination #83 comment5879008128. No other source branch is overwritten.

Regression-first: independent import/construction, one verifier owner, compatibility forwarding without business policy in Application, unchanged derivation/receipt bytes, actual PostgreSQL concurrent one-time claim and expired refusal without consuming a claim. Existing full token/credential/recovery/client/batch security suites remain. Project execution is cloud-only; source editing and artifact inspection are local.

Pending implementation: one narrow TokenDelivery owner, explicit metadata/key/clock/current-window dependencies, same service for identity forwarding and post-commit response release. Keep transaction-local recovery binding separate from the later claim commit; only then may a token enter a response. Persisted results/events/receipts remain secret-free. Preserve current settings/clock providers rather than freeze them accidentally.

Final focused/full four-shard/node-ID/conformance/build/recovery outcomes are pending. No deployment, actual credentials, Root/PIN, backup destruction, external delivery or recovery promotion. #67 stays completed and its original functional regressions remain binding; this change is ownership-only, not a new token protocol.
