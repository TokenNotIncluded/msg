"""Bounded, revision-bound presentation of explicitly published attachments."""

import asyncio
import time

from msg.core.codec import wire
from msg.core.errors import Failure, require
from msg.core.models import ResourceRef
from msg.core.post_preview import plain_text, shorten
from msg.plugins.common import check_access

MAX_ATTACHMENTS = 32
PREVIEW_BYTES = 512
PREVIEW_CHARACTERS = 180

_HIDDEN = frozenset({
    'permission_denied',
    'credential_ceiling',
    'certificate_gate',
    'tool_certificate_required',
    'delegation_scope',
    'ancestor_inactive',
    'not_found',
    'revision_not_found',
    'resource_purged',
})


def attachment_kind(media_type):
    """Active documents, including SVG and HTML, remain download-only files."""
    media = media_type.partition(';')[0].strip().lower()
    if media in {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'}:
        return 'image'
    if media in {'video/mp4', 'video/webm', 'video/ogg', 'video/mpeg', 'video/quicktime'}:
        return 'video'
    if media in {
        'audio/mpeg',
        'audio/mp4',
        'audio/ogg',
        'audio/wav',
        'audio/x-wav',
        'audio/webm',
        'audio/flac',
    }:
        return 'audio'
    if media in {'text/plain', 'text/markdown'}:
        return 'text'
    return 'file'


async def attachment_descriptors(app, ctx, request, tx, revision):
    """A readable post does not grant access to any relation target or its bytes."""
    relations = [relation for relation in revision.relations if relation.type == 'attachment']
    items = []
    for relation in relations[:MAX_ATTACHMENTS]:
        require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
        await asyncio.sleep(0)
        ref = relation.target
        unavailable = {
            'ref': wire(ref),
            'available': False,
            'name': 'Unavailable attachment',
            'kind': 'file',
        }
        item = dict(unavailable)
        try:
            # A floating relation must never silently switch to a new source version.
            require(ref.revision is not None, 'revision_not_found')
            await check_access(app, ctx, request, tx, ref.id, 'read')
            resource = await tx.resource(ref.id)
            require(resource.state != 'purged', 'resource_purged')
            require(resource.type == 'attachment', 'revision_not_found')
            target = await tx.revision(ref)
            fixed = ResourceRef(id=ref.id, revision=target.id)
            media_type = target.content.media_type
            kind = attachment_kind(media_type) if len(media_type) <= 160 else 'file'
            item.update(
                ref=wire(fixed),
                available=True,
                name=shorten(resource.name, 240),
                kind=kind,
                media_type=shorten(media_type, 160),
                size=target.content.size,
                digest=target.content.digest,
                raw_url=f'/_id/{fixed.id}/revisions/{fixed.revision}/raw',
            )
            if len(media_type) > 160:
                item['media_type_truncated'] = True
            if target.summary:
                item['summary'] = shorten(plain_text(target.summary[:1024]), 280)
            if kind == 'text' and target.content.size:
                end = min(target.content.size, PREVIEW_BYTES)
                raw = b''.join([
                    chunk async for chunk in app.contents.read(target.content, (0, end))
                ])
                preview = plain_text(raw.decode('utf-8', errors='replace'))
                item['preview'] = shorten(preview, PREVIEW_CHARACTERS)
                item['preview_truncated'] = (
                    target.content.size > end or len(preview) > PREVIEW_CHARACTERS
                )
        except Failure as exc:
            if exc.code not in _HIDDEN:
                raise
            item = unavailable
        except FileNotFoundError:
            item = unavailable
        items.append(item)
    return items, len(relations) > MAX_ATTACHMENTS
