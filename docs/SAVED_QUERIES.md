# Saved query descriptors

Use `/AGENTS.md`, the applicable rules and `/-/d` on the target server before
writing. Inspect the exact operation version with `msg schema query.save@1` or
`msg schema communication.watch_create@2`; enabled code does not extend a
credential's operation ceiling. The CLI's `msg call` uses the same signed
Operation contract as other transports.

## Save and read

`transfer.query_seal@1` issues a short-lived read ticket. It is bound to the actor,
subject and credential, expires within 15 minutes, and depends on its sealed
Transfer. Reads never extend it or imply ACK.

`query.save@1` explicitly saves an unexpired ticket into a private `saved_query`
Resource and immutable Revision. Input is `{"query_ref":"<ticket>"}`. The result
contains `resources[0]` and `data.saved_ref`, both a fixed `{id, revision}`, plus
`descriptor_digest`. Save requires the owning account as actor and subject and a
signature. Retry with the same request ID and business content to return the same
fact.

The saved descriptor pins the original read operation, contract version and all
arguments. Parent, scope, author, owner and relation endpoints are resolved to
stable IDs at save time, including structured search scopes. The digest covers
the normalized descriptor. No result page, cursor or acquired permission is
copied. The existing temporary ticket and
Transfer keep their expiry; the explicit saved copy is independent of their later
cleanup.

`query.saved_get@1` takes `{"ref":{"id":"...","revision":"..."}}` and returns
the descriptor and digest after checking the current owner, saved resource,
query scope and authority. The creation principal is internal metadata: generic
content reads, sharing, raw downloads and creation-time projections cannot expose
it. Both the saving principal and the caller must still hold current query scope
permission. Revocation, credential expiry and narrowed ceilings stop access;
saving does not extend the original credential's lifetime. This operation reads
the descriptor; it does not execute the saved query or ACK anything.

## Archive

`query.saved_archive@1` takes the same pinned ref plus the saved resource's
`expected_generations` entry in the OperationRequest. The ref must name the
current saved revision. Its owner can explicitly revoke the source even if the
creation authority or query scope has since become unavailable; archive still
requires its own current authority and a signature.

## Event watches

`communication.watch_create@2` accepts `query_ref: {id, revision}` pointing to a
saved descriptor, `event_types`, `delivery: "inbox"`, and optional `expires_at`.
It requires a fixed revision and a signature. The CLI selects this version for:

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
Only authorized references enter Inbox; no content bodies are copied and reading
Inbox does not ACK. Saving a search descriptor does not make it usable by this
watch contract. External channels are unsupported. Target-based watches use
`communication.watch_create@1` and its event vocabulary.
