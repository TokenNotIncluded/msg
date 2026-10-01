"""The community wiki is public, jointly editable and retains its revisions."""

from dataclasses import replace

from msg.core.errors import require

WIKI_ID = 't_wiki'


async def in_wiki(tx, resource):
    return resource.id == WIKI_ID or any(
        ancestor.id == WIKI_ID for ancestor in await tx.ancestors(resource.id)
    )


def protect_wiki(chain, operation):
    if not any(item.id == WIKI_ID for item in chain):
        return
    name = operation.split('@')[0]
    require(
        name
        not in {
            'content.move',
            'content.archive',
            'content.restore',
            'content.purge',
            'content.chmod',
            'content.chgrp',
            'content.chown',
            'content.topic_configure',
            'content.topic_policy_set',
            'file.delete',
            'content.topic_remove',
            'content.topic_promote',
            'content.topic_demote',
            'content.topic_ban',
            'content.topic_unban',
            'content.topic_invite',
            'content.topic_approve',
        }
        and not name.startswith('content.topic_member_'),
        'wiki_shared_resource',
    )


async def sync_wiki(tx, registry, now):
    """Upgrade existing wiki modes without rewriting content or revision history."""
    if tx.setting('wiki_policy_version') == 1:
        return
    pending = [WIKI_ID]
    while pending:
        resource = await tx.resource(pending.pop())
        container = registry.resource_type(resource.type, resource.type_version).container
        mode = 0o1777 if container else 0o666
        if resource.mode != mode:
            await tx.replace(
                replace(resource, mode=mode, generation=resource.generation + 1, modified_at=now),
                resource.generation,
            )
        if resource.type == 'topic':
            policy = tx.setting('policy:' + resource.id, {})
            shared = {
                key: value
                for key, value in policy.items()
                if key in {'rules', 'recommended_template'}
            }
            if policy != shared:
                tx.set_setting('policy:' + resource.id, shared)
            tx.execute('DELETE FROM topic_bans WHERE topic=?', (resource.id,), write=True)
            tx.execute(
                "UPDATE topic_memberships SET role='member' WHERE topic=?",
                (resource.id,),
                write=True,
            )
            tx.execute(
                "UPDATE topic_settings SET membership_policy='open' WHERE topic=?",
                (resource.id,),
                write=True,
            )
        cursor = None
        while True:
            page = await tx.children(resource.id, cursor=cursor, limit=500)
            pending.extend(child.id for child in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    tx.set_setting('wiki_policy_version', 1)
