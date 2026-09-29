# Hosting authority supplement (#161 / #173 / #174)

## Single integration path

#173 is the only product integration path. #174 is closed as superseded, not
merged or accepted as a completed #161. Its duplicate runtime/storage/publication
implementation is not stacked. Original #174 RED and failure-correction evidence
remain in that PR (comments5887238998,5887734281). Historical source
51af07f4835c37a00cf438055ca41a19f7cd6246 is retained, not deployed.

#173 head6703f3de41e988977be09e509c91fc30a11738b1 retained #174's real UID,
ACL/cache, missing-schema and quarantine tests. The independent child branch
`fix/hosting-authority-174-to-173` adds only key-revocation/owner-authority tests
and nine lines of ownership checking; it never overwrites #173. Coordination:
#173 comments5887748079,5887771169,5887792936,5887839120; #83 comment5887825824.

## Verified cloud evidence

Test commit c314d1b2ff0c892e274d731d5c424154a44f2784 added six tests. Run36551691097
failed container initialization because Actions did not group a single-quoted
Docker health command. No tests/artifacts: NOT a functional RED. Commit
99a0242bf9fdc089f61b7b127c21853e66b20a7d corrected only the workflow quoting.

**RED**: head=checkout99a0242bf9fdc089f61b7b127c21853e66b20a7d,
tree70361477198cf11e08ec9d6876ac6ccdb08415c8, run36551843100/attempt1,
artifact11024911828, Python3.15.0rc2. JUnit **6 tests:3 passed/3 failed/0 errors/0 skipped**.
Downloaded ZIP SHA256 verified against provider:
5664e005eb1cd92ba8d7f1f4e97b980d823793639426a24fb94b1aed891a9146.

The three failures are table/schema/sequence owners: the actual reader login
successfully restores its own permissions with GRANT, removes ACLs again, and
hosting.load incorrectly succeeds. Database ownership, real key revocation
(GET/HEAD/Range/ETag), and column-only UPDATE cases already passed.

**GREEN**: head=checkoutd17a2875986b9f897c266f3632f3e8681ce58a96,
treeeef4fef2bf5a19908a4a8fb8be690c80bbd2dc01, run36552196870/attempt1,
artifact11024289895, Python3.15.0rc2. JUnit **the same 6 tests:6 passed/0 failures/0 errors/0 skipped**.
Downloaded ZIP SHA256 verified against provider:
47981a0025c74219454aef9f47f8b2cb396255f8e682f39e30a125f650c5b4db.
Source manifest and every test name/result were independently inspected. This
is a focused regression result, not a claim that canonical #173 or the full
project has passed. This later note-only commit is not the tested d17 head.

## Repair and integration handoff

`storage/read_only.py::require_readonly_role` rejects current-database, schema,
relation and routine ownership before checking effective ACLs. Source diff is
**9 added/0 removed lines**. Existing table-read allowlist, canonical transactions,
authentication, signatures, release validation and GitContentReader are unchanged.
No assertions were weakened, skipped or marked as expected failures.

For integration into #173, retain the three source commits c314d1b (tests),
99a0242 (health quoting) and d17a287 (guard), plus this evidence note, or apply
their exact narrow diff with traceable attribution. Do not copy the old #174
Application/Git implementation. Reconcile against the latest #173 head and run
its full combined four-shard/node-ID/conformance/build and isolation workflows.
The canonical branch owner retains schema/marker/real-UID fixes and serial merge.

All project execution is in Actions. No production secrets, deployment,
permissions, real outbound delivery, Root/PIN, funds or recovery promotion.
#161 is not closed by this supplement; #80/#84 full acceptance remains separate.
