"""Board-local guidance and enforced membership policy travel with the board."""

from msg.core.errors import require


async def rules_projection(tx, resource):
    policy = tx.setting('policy:' + resource.id, {})
    membership = tx.one(
        'SELECT membership_policy FROM topic_settings WHERE topic=?', (resource.id,)
    )
    return {
        'board_id': resource.id,
        'generation': resource.generation,
        'text': policy.get('rules', ''),
        'posting_policy': policy.get('posting_policy', 'open'),
        'membership_policy': membership[0] if membership else 'open',
        'reply_open': policy.get('reply_open', True),
        'editable': policy.get('editable', True),
        'platform_rules': '/_rules/topics',
    }


async def enforce_rules(tx, ctx, request, parent):
    policy = tx.setting('policy:' + parent, {})
    posting = policy.get('posting_policy', 'open')
    if posting == 'open':
        return
    member = tx.one(
        'SELECT role,status FROM topic_memberships WHERE topic=? AND subject=?',
        (parent, ctx.principal.subject),
    )
    require(member is not None and member[1] == 'active', 'board_members_only')
    if posting == 'admins':
        require(member[0] == 'admin', 'board_admins_only')
