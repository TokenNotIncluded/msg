# Post attachments

Posts can publish files from an existing, readable source revision. A normal post
read returns small attachment descriptors instead of embedding the attached file
as binary or base64 data. Text attachments can include a bounded preview.

This uses the existing `content.attach@1`, Resource/Revision model and transfer
operations. Inspect the target deployment's operation dictionary and schema
before writing; installing newer source does not expand a credential's authority.

## Upload and publish one fixed version

Upload the source file, then create a short post explaining what readers should
open. These example commands perform writes when executed:

```sh
msg schema content.attach@1
msg --format json upload ./diagram.png --media-type image/png
msg --format json call content.post_create \
  '{"parent":"/main","body":"Architecture diagram attached. Open the file for the full image."}'
```

The upload result's `output` is a source reference with `id` and `revision`. The
post result's `resources[0]` identifies the new post and its revision;
`data.generation` is the generation needed for the next write. Replace every
uppercase placeholder below with the returned value:

```sh
msg --format json call content.attach --contract-version 1 \
  '{"post":"POST_ID","source":{"id":"SOURCE_ID","revision":"SOURCE_REVISION"},"name":"diagram.png"}' \
  --expect POST_ID=POST_GENERATION
```

Use both source ID and source revision. The publisher must currently be able to
read that source, write the post and create an attachment in the post's Topic.
If another write changed the post generation, read its metadata again and review
the updated post before retrying with the new generation. Retry an uncertain
request with `--request-id ORIGINAL_REQUEST_ID` and the same business content.

`content.attach` creates a separate attachment Resource and returns its fixed
reference in `data.attachment`. It creates a new post Revision whose attachment
relation pins that attachment Revision. The post body bytes stay unchanged;
earlier post Revisions keep their original body and relation list. Later changes
to the original source file do not replace the published attachment's bytes.
For another attachment write, use the returned post generation and new post
Revision instead of the values from the original post creation.

The original source keeps its own permissions. The published attachment starts
with the post's permission mode and lives in the post's Topic. Publishing from a
private original therefore publishes a separate copy with the post's mode; it
does not make the original public. The published Resource has its own current
access checks. Changing the original's permissions alone does not withdraw that
published copy.

## What a post read returns

```sh
msg --format json read POST_ID
msg --format json read POST_ID --revision POST_REVISION
```

The selected post Revision supplies the attachment relations. Each default read
returns at most 32 descriptors in `attachments`; `attachments_more` reports
whether that Revision has additional attachment relations.

A currently readable, fixed attachment reference can return:

- `ref`: the attachment's `id` and pinned `revision`.
- `available: true`, `name`, `media_type`, byte `size` and content `digest`.
- `kind`: `image`, `video`, `audio`, `text` or `file`, as a presentation hint.
- `raw_url`: an explicit raw URL pinned to that attachment Revision.
- An optional plain-text Revision `summary`, limited to 280 characters.
- For plain-text or Markdown files, a `preview` made from at most 512 source
  bytes and limited to 180 characters, with `preview_truncated` when shortened.

Names are limited to 240 characters. Media types are limited to 160 characters;
an overlong value sets `media_type_truncated: true` and uses `kind: file`.

The descriptor does not automatically fetch the raw file, decode an image or
load audio/video. Read permission is checked on every target using its current
resource ancestry, even when the post or attachment Revision is historical.

An unreadable or missing target returns only this generic descriptor:

```json
{
  "ref": {"id": "ATTACHMENT_ID", "revision": "ATTACHMENT_REVISION"},
  "available": false,
  "name": "Unavailable attachment",
  "kind": "file"
}
```

It contains no real filename, media type, size, digest, summary, preview, body or
raw URL. Reading a post does not grant access to its relation targets and does not
submit an ACK.

## Metadata, bounded text and complete files

Use metadata when only identity, size, type, digest or the current generation is
needed. This does not request the attachment descriptors or body:

```sh
msg --format json read POST_ID --meta
msg --format json read ATTACHMENT_ID --meta
```

Metadata describes the current Resource. Use the pinned attachment descriptor
for the published version's size, media type and digest.

A regular `msg read` keeps the normal body projection: readable text that fits
the server's inline limit can be returned in full. It is not a general-purpose
text preview command. To read a long text body in bounded pieces, use the existing
segment operation with a fixed Revision:

```sh
msg --format json call discovery.read_segment \
  '{"id":"ATTACHMENT_ID","revision":"ATTACHMENT_REVISION","max_bytes":4096}'
```

The result has byte `range`, `text` and block-continuation information. For the
CLI, copy the opaque token after `/_r/c/` in the returned `next` reference and
send only that cursor with the same identity:

```sh
msg --format json call discovery.read_segment \
  '{"cursor":"TOKEN_FROM_RETURNED_NEXT"}'
```

Do not add `id`, `revision` or `max_bytes` to the continuation, construct offsets,
or alter the bound query. The default segment size is 4096 bytes, with an allowed
maximum of 8192 bytes subject to the deployment's response limit. Each
continuation rechecks current access. Segment reads require text content.

For the full original attachment, explicitly open the descriptor's pinned
`raw_url`, or download the published attachment reference to a local file:

```sh
msg download ATTACHMENT_ID ./diagram-downloaded.png \
  --revision ATTACHMENT_REVISION
```

Download uses resumable transfer chunks, verifies chunk and final content
digests, and refuses to overwrite an existing destination. Raw reads support
HEAD and byte ranges, return a download filename, and recheck current access.
Use the published attachment's reference for shared readers; the private source
reference still requires access to the original source.
