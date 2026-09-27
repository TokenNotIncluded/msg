"""Revocable, direct-subject read grants for one resource at a time."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac

from msg.constants import ROOT_SUBJECT
from msg.core.codec import b64, parse_time, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, ResourceRef
from msg.plugins.common import check_access, new_id, registration, resolve
from msg.plugins.schemas import IDENTIFIER, STRING, obj


MAX_GRANT_TTL = timedelta(days=30)
MAX_LINK_BYTES = 65536


def _link_public(row):
    return dict(zip(('id', 'resource_id', 'grantor', 'created_at', 'expires_at',
                     'revoked_at'), row))


def _links_enabled(tx):
    require(tx.setting('share_links_enabled', False) is True, 'share_links_disabled')


def _verifier(token):
    try:
        raw = unb64(token, limit=64)
    except (Failure, ValueError):
        raise Failure('share_link_unavailable') from None
    require(32 <= len(raw) <= 64, 'share_link_unavailable')
    return 'sha256:'+hashlib.sha256(b'share-link-v1\0'+raw).hexdigest()


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
    require(not any(tx.setting('hosting_preview:'+item.id) or
                    tx.setting('hosting_preview_file:'+item.id) for item in chain),
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

    @op('sharing.link_create', obj({'resource': IDENTIFIER,
        'verifier': {'type':'string','pattern':'^sha256:[0-9a-f]{64}$'},
        'expires_at': STRING}, ('resource','verifier','expires_at')), signature=True)
    async def link_create(ctx, request, tx):
        _links_enabled(tx)
        require(ctx.principal.method=='signature' and
                ctx.principal.actor==ctx.principal.subject, 'share_subject_required')
        a=request.arguments
        resource=await _owned_resource(app,ctx,request,tx,a['resource'],mutation=True)
        expires=parse_time(a['expires_at'])
        require(ctx.now<expires<=ctx.now+MAX_GRANT_TTL,'invalid_share_expiry')
        require(resource.revision is not None,'share_link_content_required')
        revision=await tx.revision(ResourceRef(id=resource.id))
        require(revision.content.size<=MAX_LINK_BYTES,'share_link_content_too_large')
        require(tx.one('SELECT 1 FROM share_links WHERE verifier=?',(a['verifier'],)) is None,
                'share_link_verifier_in_use')
        lid=new_id('link')
        tx.execute('''INSERT INTO share_links
            (id,resource_id,grantor,credential_id,verifier,created_at,expires_at,revoked_at)
            VALUES (?,?,?,?,?,?,?,NULL)''',
            (lid,resource.id,resource.owner,ctx.principal.credential_id,a['verifier'],
             wire(ctx.now),wire(expires)),write=True)
        tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)+1)
        return HandlerOutput(data={'link':_link_public((lid,resource.id,resource.owner,
            wire(ctx.now),wire(expires),None)),
            'endpoint':'/-/p/sharing.link_read',
            'proof':'client-held token in JSON request body'})

    @op('sharing.link_revoke',obj({'link_id':IDENTIFIER},('link_id',)),signature=True)
    async def link_revoke(ctx,request,tx):
        lid=request.arguments['link_id']
        row=tx.one('''SELECT id,resource_id,grantor,created_at,expires_at,revoked_at
            FROM share_links WHERE id=?''',(lid,))
        require(row is not None,'share_link_unavailable')
        resource=await _owned_resource(app,ctx,request,tx,row[1],mutation=True)
        require(ctx.principal.method=='signature' and row[2]==resource.owner,
                'permission_denied')
        if row[5] is None:
            tx.execute('UPDATE share_links SET revoked_at=? WHERE id=?',
                       (wire(ctx.now),lid),write=True)
            tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)+1)
            row=(*row[:5],wire(ctx.now))
        return HandlerOutput(data={'link':_link_public(row)})

    @op('sharing.link_list',obj({'resource':IDENTIFIER},('resource',)),effect='read')
    async def link_list(ctx,request,tx):
        resource=await _owned_resource(app,ctx,request,tx,
                                       request.arguments['resource'],mutation=False)
        rows=tx.rows('''SELECT id,resource_id,grantor,created_at,expires_at,revoked_at
            FROM share_links WHERE resource_id=? AND grantor=? ORDER BY created_at,id''',
            (resource.id,resource.owner))
        return HandlerOutput(data={'links':[_link_public(row) for row in rows]})

    @op('sharing.link_read',obj({'link_id':IDENTIFIER,'token':STRING},
                                ('link_id','token')),effect='read')
    async def link_read(ctx,request,tx):
        # Never accept signed identity as an alternative to the bearer source.
        require(ctx.principal.method=='anonymous','share_link_unavailable')
        _links_enabled(tx)
        a=request.arguments
        verifier=_verifier(a['token'])
        row=tx.one('''SELECT resource_id,grantor,credential_id,verifier,expires_at,revoked_at
            FROM share_links WHERE id=?''',(a['link_id'],))
        require(row is not None and hmac.compare_digest(row[3],verifier) and
                row[5] is None and parse_time(row[4])>ctx.now,'share_link_unavailable')
        resource=await tx.resource(row[0])
        chain=(*await tx.ancestors(resource.id),resource)
        require(await app.authorizer.share_link_read_allowed(row[1],row[2],resource,
            chain,ctx.now,tx),'share_link_unavailable')
        revision=await tx.revision(ResourceRef(id=resource.id))
        require(revision.content.size<=MAX_LINK_BYTES,'share_link_unavailable')
        body=await app.contents.read_bytes(revision.content,limit=MAX_LINK_BYTES)
        return HandlerOutput(data={'id':resource.id,'revision':revision.id,
            'media_type':revision.content.media_type,'data':b64(body)})

    finish()
