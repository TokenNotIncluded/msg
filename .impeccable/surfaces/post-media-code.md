---
version: 1
slug: "post-media-code"
primary_target: "src/msg/transports/attachment_views.py"
related_targets: ["src/msg/transports/code_views.py", "src/msg/transports/revision_diff.py", "src/msg/plugins/attachment_projection.py", "src/msg/plugins/discovery.py", "src/msg/core/revision_diff.py"]
---

# Reading posts, attachments, code and version differences

## Scope and mode

Mode: Read. Primary targets: `src/msg/transports/attachment_views.py`,
`code_views.py` and `revision_diff.py`; related source contracts are
`src/msg/plugins/attachment_projection.py`, `src/msg/plugins/discovery.py`
and `src/msg/core/revision_diff.py`.

## Reading material — 2026-10-03

Ordinary post/document bodies keep the existing light reading language and
system/light/dark preferences. Attachments form a charcoal, fine-bordered island
with mono captions and readable blue download links. A bounded internal list
keeps a long attachment set from taking over the page. This is not a site-wide
dark redesign. `gadgets.muse.ai` supplies a material/border reference only.

Attachment descriptors expose at most 32 explicitly published relation targets.
Each descriptor is bound to an exact readable revision and contains bounded
metadata, not an inline copy of arbitrary source bytes. Text previews read at
most 512 bytes and show at most 180 characters; summaries are at most 280
characters. Long text can be referenced as an attachment and opened separately.
Access is checked for the target itself; a readable post does not grant access
to its attachments. Unavailable targets stay unavailable instead of showing
metadata or silently switching revisions.

## Native media and safe files

Human views render allowed raster images, including GIF, with native images.
Allowed video/audio use native controls and `preload="none"`, without autoplay;
video uses `playsinline`. Every available item retains a download link. SVG,
HTML and other active documents are download-only, never embedded. Text/plain
and Markdown offer a short collapsed preview and an original read link. Raw
links must match the exact same-origin, revision-bound path in the descriptor.
Markdown/Agent reads retain attachment references and bounded metadata.

## Code and version differences

Fenced code shows visible line numbers and locally generated highlighting.
Unknown or invalid language labels remain escaped plain text. Numbers are
separate from source content; Copy source preserves the original source, with
a selectable fallback and exact-source download when clipboard access is unavailable. The code region
can scroll horizontally and remains keyboard accessible.

Diff pages show old/new line numbers, distinct added/removed rows and explicit
server continuation links. Structured `diff_lines` carry exact counters even
when pagination begins mid-hunk. Copy diff uses that page's exact `diff` text;
it does not claim to copy unseen pages. The existing `diff` field and content
API remain compatible, and current access is checked before every read. A first
revision explains that there is no earlier revision rather than inventing a
comparison. No reading action generates an ACK.

## Release status

This brief documents the local media/code/diff iteration prepared on
`384e9b3211651591ade0325aa665e3a2896ff7b8`, with the subsequently reviewed
exact-copy fallback fix. Publication awaits native45 and is reported separately.
This documentation introduces no UI or protocol change and does not add a new
browser, production or physical-device acceptance claim.
