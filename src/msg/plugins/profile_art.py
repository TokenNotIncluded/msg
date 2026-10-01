"""Tiny artwork references only; API reads never load or generate SVG bytes."""

from urllib.parse import quote

from msg.core.models import ResourceRef
from msg.core.profile_art import MAX_SVG_BYTES


async def profile_artwork(app, ctx, request, tx, subject_id):
    from msg.plugins.discovery import visible

    path = quote('/' + (await tx.resource(subject_id)).name, safe='/@')
    artwork = {}
    for kind, name in (
        ('avatar', 'AVATAR.svg'),
        ('background', 'BACKGROUND.svg'),
        ('footer', 'FOOTER.svg'),
    ):
        custom = None
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
                    custom = path + '/' + name
        artwork[kind] = {'url': path + '/art/' + kind + '.svg', 'file': custom}
    return artwork
