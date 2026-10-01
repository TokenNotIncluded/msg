"""Topic presentation uses ordinary versioned files with board-manager authority."""

from urllib.parse import quote

from msg.constants import ROOT_SUBJECT
from msg.core.board_art import board_default
from msg.core.errors import Failure, require

FILES = {'ABOUT.md', 'HEADER.svg'}


async def is_channel(tx, resource):
    return (
        resource.type == 'topic'
        and not tx.one(
            'SELECT resource_id FROM dm_conversations WHERE resource_id=?', (resource.id,)
        )
        and not any(item.type == 'user' for item in await tx.ancestors(resource.id))
    )


def is_admin(tx, topic, subject):
    if subject == ROOT_SUBJECT:
        return True
    row = tx.one(
        'SELECT role,status FROM topic_memberships WHERE topic=? AND subject=?', (topic, subject)
    )
    return row == ('admin', 'active')


def can_edit(tx, topic, subject):
    return bool(subject and (subject == topic.owner or is_admin(tx, topic.id, subject)))


async def presentation_topic(tx, resource):
    if resource.type == 'file' and resource.name in FILES and resource.parent:
        parent = await tx.resource(resource.parent)
        if parent.type == 'topic':
            return parent
    return None


async def projection(app, ctx, request, tx, resource):
    from msg.plugins.common import check_access

    description, _ = board_default(resource.name)
    files = {}
    for name in sorted(FILES):
        row = tx.one(
            "SELECT id FROM resources WHERE parent=? AND name=? AND type='file' AND state='active'",
            (resource.id, name),
        )
        if row:
            item = await tx.resource(row[0])
            try:
                await check_access(app, ctx, request, tx, item.id, 'read')
            except Failure:
                continue
            files[name] = item.id
            if name == 'ABOUT.md':
                from msg.core.models import ResourceRef

                revision = await tx.revision(ResourceRef(id=item.id, revision=item.revision))
                if (
                    revision.content.media_type in {'text/markdown', 'text/plain'}
                    and revision.content.size <= 4096
                ):
                    description = (
                        await app.contents.read_bytes(revision.content, limit=4096)
                    ).decode('utf-8', errors='replace')
    # Root is a local administrator, independent of mutable topic memberships.
    managers = [ROOT_SUBJECT]
    managers.extend(
        row[0]
        for row in tx.rows(
            "SELECT subject FROM topic_memberships WHERE topic=? AND role='admin' AND status='active' ORDER BY subject",
            (resource.id,),
        )
        if row[0] != ROOT_SUBJECT
    )
    manager_names = [
        '@root' if subject == ROOT_SUBJECT else (await tx.resource(subject)).name
        for subject in managers
    ]
    return {
        'administrator_names': manager_names,
        'description': description,
        'administrators': managers,
        'header': {
            'url': '/_board/' + quote(resource.id, safe='') + '/header.svg',
            'file': files.get('HEADER.svg'),
        },
        'description_file': files.get('ABOUT.md'),
    }


async def protect_edit(tx, ctx, resource):
    topic = await presentation_topic(tx, resource)
    if topic:
        require(can_edit(tx, topic, ctx.principal.subject), 'topic_admin_required')
    return topic
