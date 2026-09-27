"""Revocable, direct-subject read grants for one resource at a time."""
from __future__ import annotations

from datetime import timedelta

from msg.constants import ROOT_SUBJECT
from msg.core.codec import parse_time, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput
from msg.plugins.common import check_access, new_id, registration, resolve
from msg.plugins.schemas import IDENTIFIER, STRING, obj


MAX_GRANT_TTL = timedelta(days=30)


async def _owned_resource(app, ctx, request, tx, value, *, mutation):
    rid = await resolve(tx, value)
    resource = await tx.resource(rid)
    require(ctx.principal.subject is not None and ctx.principal.actor == ctx.principal.subject,
            'share_subject_required')
    require(resource.owner == ctx.principal.subject and resource.state == 'active',
            'permission_denied')
    require(not app.registry.resource_type(resource.type, resource.type_version).container,
            'share_container_forbidden')
    await check_access(app, ctx, request, tx, rid, 'manage' if mutation else 'read')
    # A grant cannot cross system policy, private conversation, tool, credential,
    # last-will, or subject bootstrap boundaries. DM is also enforced at read time.
    chain = (*await tx.ancestors(rid), resource)
    require(not any(item.id in {'r_agents', 'r_rules', 't_last_will'} or
                    item.type in {'tool', 'csr', 'certificate', 'credential',
                                  'legacy_directive', 'dm_conversation'}
                    for item in chain), 'share_forbidden_resource')
    require(not any(parent.type == 'user' and child.name in {'SOUL.md', 'AGENTS.md', 'todos'}
                    for parent, child in zip(chain, chain[1:])), 'share_forbidden_resource')
    require(tx.one('''SELECT 1 FROM dm_conversations WHERE resource_id IN ('''+
                   ','.join('?' for _ in chain)+') LIMIT 1', tuple(item.id for item in chain)) is None,
            'share_forbidden_resource')
    return resource


def _public(row):
    return dict(zip(('id', 'resource_id', 'grantor', 'grantee', 'created_at', 'expires_at',
                     'revoked_at'), row))


def install(app):
    op, finish = registration(app, 'sharing', ('identity',))

    @op('sharing.grant', obj({'resource': IDENTIFIER, 'grantee': IDENTIFIER,
                              'expires_at': STRING}, ('resource', 'grantee', 'expires_at')))
    async def grant(ctx, request, tx):
        a = request.arguments
        resource = await _owned_resource(app, ctx, request, tx, a['resource'], mutation=True)
        grantee = await resolve(tx, a['grantee'])
        await tx.subject(grantee)
        require(grantee not in {resource.owner, ROOT_SUBJECT}, 'invalid_share_grantee')
        expires = parse_time(a['expires_at'])
        require(ctx.now < expires <= ctx.now + MAX_GRANT_TTL, 'invalid_share_expiry')
        tx.execute('''UPDATE share_grants SET revoked_at=?
            WHERE resource_id=? AND grantee=? AND revoked_at IS NULL AND expires_at<=?''',
                   (wire(ctx.now), resource.id, grantee, wire(ctx.now)), write=True)
        require(tx.one('''SELECT 1 FROM share_grants
            WHERE resource_id=? AND grantee=? AND revoked_at IS NULL''',
                       (resource.id, grantee)) is None, 'share_already_active')
        gid = new_id('share')
        tx.execute('''INSERT INTO share_grants
            (id,resource_id,grantor,grantee,created_at,expires_at,revoked_at)
            VALUES (?,?,?,?,?,?,NULL)''',
            (gid, resource.id, resource.owner, grantee, wire(ctx.now), wire(expires)), write=True)
        tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)+1)
        return HandlerOutput(data={'grant': _public((gid, resource.id, resource.owner,
            grantee, wire(ctx.now), wire(expires), None))})

    @op('sharing.revoke', obj({'grant_id': IDENTIFIER}, ('grant_id',)))
    async def revoke(ctx, request, tx):
        gid = request.arguments['grant_id']
        row = tx.one('''SELECT id,resource_id,grantor,grantee,created_at,expires_at,revoked_at
            FROM share_grants WHERE id=?''', (gid,))
        require(row is not None, 'share_not_found')
        resource = await _owned_resource(app, ctx, request, tx, row[1], mutation=True)
        require(row[2] == resource.owner, 'permission_denied')
        if row[6] is None:
            tx.execute('UPDATE share_grants SET revoked_at=? WHERE id=?',
                       (wire(ctx.now), gid), write=True)
            tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)+1)
            row = (*row[:6], wire(ctx.now))
        return HandlerOutput(data={'grant': _public(row)})

    @op('sharing.list', obj({'resource': IDENTIFIER}, ('resource',)), effect='read')
    async def list_grants(ctx, request, tx):
        resource = await _owned_resource(app, ctx, request, tx,
                                         request.arguments['resource'], mutation=False)
        rows = tx.rows('''SELECT id,resource_id,grantor,grantee,created_at,expires_at,revoked_at
            FROM share_grants WHERE resource_id=? AND grantor=? ORDER BY created_at,id''',
                       (resource.id, resource.owner))
        return HandlerOutput(data={'grants': [_public(row) for row in rows]})

    finish()
