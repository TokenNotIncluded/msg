# SQLite 0.22.1 historical identity records

`msg.storage.legacy_identity_plan` inventories a protected offline snapshot using explicit public-column projections. It never selects old token hashes, custody ciphertext/key nonces, keystore blobs or webhook secrets. It hashes the snapshot before and after reading. No old service code is executed and no live database is modified.

The post create/edit signature format and certificate canonicalization were compared to the installed public `msgd/crypto.py`, SHA-256 `235d2736927dbdf2a0d37cd28d78b4b46324b6aba902461f9eca5b1ec22fc83e`, an exact match to `e12eea32764656eec82452fee111e5b4fb6d8a42:src/msgd/crypto.py`. Signature verification proves only that the reconstructed historical bytes were signed by that public key. It does not establish current owner access or v4 authority. Edited/deleted historical content or lost attachment manifests may make an old signature impossible to reconstruct; an invalid result must not be silently upgraded to verified.

```
python -m msg.storage.legacy_identity_plan --snapshot /protected/old.sqlite \
  --sha256 EXPECTED_HASH --output /protected/identity-plan.json \
  --root-public-key /protected/independently-verified-old-root.pub
```

Root public-key input is optional. Without an explicit anchor, the certificate chain ends with `root_anchor_unknown`. An anchor observed on the same old server proves consistency with that server, not independent continuity; the operator must establish its provenance separately. Revoked, expired, not-yet-valid, malformed, missing/cyclic-chain and invalid-signature certificates remain classified separately. `signature_chain_verified` does not validate inherited grant/delegation policy and never enables any authority. Revocation state is the snapshot's observed state, not an independent current checkpoint.

Plans contain old identity identifiers and public-key fingerprints. Output is exclusively created with mode 0600; stdout contains only counts and digest. Keep the plan in the protected migration directory. Anonymous and conflicting-key records are explicit, and unproven IDs remain unproven.

`import_identity_records` accepts a destination Root signature over `approval_payload`: plan digest, target service, private parent plus generation, registered operator, explicit complete mapping, and expiry (at most 24 hours). Each old ID maps either to null (retain an unmapped record) or an existing registered subject. That mapping is a Root-selected historical association, **not private-key ownership proof**.

Import creates only private ordinary files under an operator-owned mode-0700 parent. It does not create Subject, credential, identity key, certificate or job records. Files have mode 0600 and do not allow login. No old tokens, CA grants, SSH permissions or custody secrets are restored. The approval/signature and report are persisted transactionally; replay and stale parent generations are rejected.

The operator must decide the destination service/operator/private parent and every mapping, review invalid or unknown evidence, establish the old Root public anchor and fresh independent revocation checkpoint, and sign the exact plan digest. New login enrollment, ownership proof, grant issuance and any public publication require their own current protocol actions. Neither a valid old signature nor this Root archive approval performs those actions.

This API is independent of `legacy_resource_import`; its records can retain previously unknown authors without manufacturing active identities. Content import still uses its own Root-approved historical mapping and must not treat these ordinary file IDs as registered subjects.
