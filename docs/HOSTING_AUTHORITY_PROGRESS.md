# Hosting authority supplement (#161 / #173 / #174)

## Consolidation

#173 is the single product integration path. #174 is not to be merged: its
runtime/storage/publication implementation is superseded, not stacked. Original
#174 RED evidence and notes remain linked in that PR (comment5887238998 and
comment5887734281). The clean historical source ends at
51af07f4835c37a00cf438055ca41a19f7cd6246; no production deployment occurred.

#173 head6703f3de41e988977be09e509c91fc30a11738b1 already retains #174's distinct-UID,
ACL/cache, missing-schema and quarantine cases. This separate child branch adds
only the not-yet-transferred authority cases, without overwriting #173. Coordination:
#173 comments5887748079 and5887771169. Base tree bc9358a0cdc3a9dc9e771ae8c748c815a86475ee.

## Test-first change

Real key replacement/revocation rejects previously accepted private-preview
signatures before GET/HEAD/Range/ETag. Column-only UPDATE is rejected. For table,
schema, sequence and database ownership, tests first prove a login can GRANT its
own rights back after REVOKE, remove those ACL bits again, then require hosting to
reject the owner. Database ownership is restored in fixture cleanup.

All project execution is GitHub Actions. The dedicated workflow saves exact head,
checkout/tree, runtime and JUnit. Tests are not yet claimed green or red until
artifacts are read. Full combined CI and #173's other existing failures remain
that branch's merge gate; this supplement cannot close #161 by itself.

No production credentials, permission changes, live delivery, Root/PIN, payment
or recovery promotion. PostgreSQL objects and signatures are disposable fixtures.
