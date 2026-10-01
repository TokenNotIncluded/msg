"""Artwork is stored as ordinary profile files and follows their normal ACLs."""

from msg.core.models import ResourceRef
from msg.core.profile_art import MAX_SVG_BYTES, generate_svg, safe_svg


async def profile_artwork(app, ctx, request, tx, subject_id):
    from msg.plugins.discovery import visible

    artwork = {}
    for kind, name in (('avatar', 'AVATAR.svg'), ('background', 'BACKGROUND.svg')):
        svg = None
        row = tx.one(
            "SELECT id FROM resources WHERE parent=? AND name=? AND state='active'",
            (subject_id, name),
        )
        if row and await visible(app, ctx, request, tx, row[0]):
            resource = await tx.resource(row[0])
            if resource.revision:
                revision = await tx.revision(ResourceRef(id=resource.id))
                blob = revision.content
                if blob.media_type == 'image/svg+xml' and blob.size <= MAX_SVG_BYTES:
                    raw = b''.join([chunk async for chunk in app.contents.read(blob)])
                    svg = safe_svg(raw)
        artwork[kind] = {
            'svg': svg or generate_svg(subject_id, kind),
            'source': 'custom' if svg else 'generated',
        }
    return artwork
