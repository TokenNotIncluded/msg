# Hosting authority supplement (#161 / #173 / #174)

## Single integration path

#173 is the only product integration path. #174 is not to be merged: its duplicate
runtime/storage/publication implementation is superseded. Original #174 RED and
failure-correction evidence remain in that PR (comments5887238998,5887734281).
Historical clean source51af07f4835c37a00cf438055ca41a19f7cd6246 is retained, not deployed.

#173 head6703f3de41e988977be09e509c91fc30a11738b1 already retained #174's real UID,
ACL/cache, missing-schema and quarantine tests. This independent child branch
`fix/hosting-authority-174-to-173` adds only key-revocation/owner-authority cases
and one narrow ownership guard. It never overwrites #173. Coordination is in
#173 comments5887748079,5887771169,5887792936 and5887839120; #83 comment5887825824.

## Actual cloud evidence

Test commit c314d1b2ff0c892e274d731d5c424154a44f2784 added six tests. Its first run
36551691097 failed container initialization because Actions did not group a
single-quoted Docker health command. No test ran and no artifact existed; this
is NOT a functional RED. Commit99a0242bf9fdc089f61b7b127c21853e66b20a7d corrected
the workflow to the established double-quoted form without changing assertions.

Valid RED: head=checkout99a0242bf9fdc089f61b7b127c21853e66b20a7d,
tree70361477198cf11e08ec9d6876ac6ccdb08415c8, run36551843100/attempt1,
artifact11024911828, Python3.15.0rc2. JUnit **6 tests:3 passed/3 failed/0 errors/0 skipped**.
ZIP downloaded and SHA256 verified against provider:
5664e005eb1cd92ba8d7f1f4e97b980d823793639426a24fb94b1aed891a9146.

The three failures are table/schema/sequence owners: the actual reader login
successfully restores its own permissions with GRANT, removes ACLs again, and
hosting.load incorrectly succeeds. Database ownership, real key revocation
(GET/HEAD/Range/ETag), and column-only UPDATE cases already passed.

## Minimal repair

`storage/read_only.py::require_readonly_role` additionally rejects current-database,
schema, relation and routine ownership before checking effective ACLs. The existing
read-table allowlist, canonical transactions, signature/authentication, source
validation and GitContentReader are unchanged. The same six assertions must turn
green; no test is skipped or converted into an expected failure.

Project execution is only in GitHub Actions. This commit has no green claim until
its report is read. #173's full combined CI, remaining schema/UID repairs and
exact merge-tree verification are still mandatory. This supplement alone cannot
close #161, #80 or #84. No production secrets, deployment, permissions, real
outbound delivery, Root/PIN, funds or recovery promotion were touched.
