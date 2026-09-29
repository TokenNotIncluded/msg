# Saved query descriptors

`transfer.query_seal@1` remains a short-lived read ticket. It is bound to the actor,
subject and credential, expires within 15 minutes, and depends on its sealed
Transfer. Reads never extend it.

`query.save@1` explicitly saves an unexpired ticket into a private `saved_query`
Resource and immutable Revision. Input is `{"query_ref":"<ticket>"}`. The result
contains `resources[0]` and `data.saved_ref`, both a fixed `{id, revision}`, plus
`descriptor_digest`. Saving the same request again returns the same fact.

The saved descriptor pins the original read operation, contract version and all
arguments. Resource selectors such as parent, scope and author are resolved to
stable IDs at save time. The digest covers the normalized descriptor. No result
page, cursor or acquired permission is copied. The existing temporary ticket and
Transfer keep their expiry; the explicit saved copy is independent of their later
cleanup.

`query.saved_get@1` takes `{"ref":{"id":"...","revision":"..."}}` and returns
the descriptor and digest after checking the current owner, saved resource,
query scope and authority. The creation principal is internal metadata: generic
content reads, sharing, raw downloads and creation-time projections cannot expose
it. Both the saving principal and the caller must still hold current query scope
permission. Revocation, credential expiry and narrowed ceilings stop access;
saving does not extend the original credential's lifetime.

`query.saved_archive@1` takes the same pinned ref plus the saved resource's
`expected_generations` entry. Its owner can explicitly revoke the source even if
the creation authority or query scope has since become unavailable.

A watch consumer must pin this exact saved revision and validate the saved
principal and its own current principal for every matching resource. It must
reject unsupported predicates explicitly. Saving a search descriptor does not
imply that every search predicate is supported by a particular watch version.

## Event watches

`communication.watch_create@2` accepts `query_ref: {id, revision}` pointing to a
saved descriptor, `event_types`, `delivery: "inbox"`, and optional `expires_at`.
It requires a signature. The CLI uses this version for:

```sh
msg watch create --query-ref QUERY_ID --query-revision REVISION_ID --event content.post_create
```

This version accepts `discovery.read_query@1` descriptors containing only
`parent`, `type`, `author`, `state`, and `tag`. `parent` is required. It rejects
search descriptors, text queries, pagination, ordering, selected fields and
nested expansion rather than silently changing their meaning. Matching shares
the read query's membership predicates and considers only references on the
current Event. It never executes a stored query or scans an old result page.
Supported events are `content.post_create`, `content.post_edit`,
`discussion.reply`, `content.archive`, `content.tags_set`, `content.move`, and
`content.chown`.

Each delivery rechecks the fixed saved source, both current principals' read
permissions and the watch operation's current authority. Source archival,
credential revocation or expiry, lost permissions and watch expiry stop delivery.
Only authorized references enter Inbox; no content bodies are copied. External
channels are unsupported by this version. Existing target watches remain on
`communication.watch_create@1` with their original event vocabulary.
