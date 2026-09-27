"""A catalog and immutable deposits; no purchase or escrow operations live here."""
from __future__ import annotations

from msg.core.codec import canonical, digest, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, ResourceRef, ResourceTypeSpec
from msg.plugins.common import (assert_generation, check_access, create_resource,
                                new_id, operation_id, registration, resolve,
                                revise_resource)
from msg.plugins.schemas import IDENTIFIER, INTEGER, REF, STRING, obj


KINDS = ('file', 'text', 'secret', 'bundle', 'entitlement', 'service')
MODES = ('managed_instant', 'sealed_manual', 'service')
STATES = ('draft', 'active', 'paused', 'closed')
MAX_REFS = 32
MAX_MANIFEST_BYTES = 65536


def _subject(ctx):
    subject = ctx.principal.subject
    require(subject is not None and ctx.principal.actor == subject and
            ctx.principal.method == 'signature', 'signature_required')
    return subject


async def _listing(app, ctx, request, tx, value, *, seller=False):
    try:
        rid = await resolve(tx, value)
        resource = await tx.resource(rid)
        require(resource.type == 'listing' and resource.parent == 't_store', 'listing_not_found')
        await check_access(app, ctx, request, tx, rid, 'read')
        if seller:
            require(resource.owner == _subject(ctx), 'listing_not_found')
        return resource
    except Failure as exc:
        if exc.code in {'not_found', 'permission_denied', 'credential_ceiling',
                        'certificate_gate', 'ancestor_inactive'}:
            raise Failure('listing_not_found') from None
        raise


async def _body(app, tx, resource, revision=None):
    rev = await tx.revision(ResourceRef(id=resource.id, revision=revision))
    body = loads(await app.contents.read_bytes(rev.content))
    # Listings deposited before the sale/bounty split were all sale listings.
    return {'mode': 'sale', **body}, rev


def _public(resource, body):
    # A package identifier is not a capability; package payloads are seller-only.
    return {'listing_id': resource.id, 'listing_revision': resource.revision,
            'seller': resource.owner, **body,
            'terms_revision': resource.revision,
            'terms_digest': digest(body['terms'])}


def _validate(body, now):
    require(body['mode'] == 'sale', 'listing_mode_unsupported')
    require(body['currency_id'] == 'primary', 'unsupported_currency')
    require(type(body['price_minor']) is int and 0 < body['price_minor'] <= 10**18,
            'invalid_price')
    require(type(body['quantity']) is int and 0 < body['quantity'] <= 10**9,
            'invalid_quantity')
    require(len(canonical(body)) <= MAX_MANIFEST_BYTES, 'listing_too_large')
    if body['expires_at'] is not None and body['state'] != 'closed':
        require(parse_time(body['expires_at']) > now, 'listing_expired')
    if body['delivery_mode'] == 'service':
        require(body['item_kind'] == 'service' and body['package_ref'] is None,
                'invalid_delivery_mode')
    else:
        require(body['item_kind'] != 'service', 'invalid_delivery_mode')
    if body['state'] == 'active' and body['delivery_mode'] == 'managed_instant':
        require(body['package_ref'] is not None, 'package_required')


async def _package_row(tx, package_id):
    return tx.one('''SELECT id,listing_id,listing_revision,seller,revision,kind,manifest,
        payload_refs,digest,total_size,delivery_mode,deposited_at FROM store_packages WHERE id=?''',
        (package_id,))


def _package_public(row):
    return dict(zip(('id', 'listing_id', 'listing_revision', 'seller', 'revision',
                     'kind', 'manifest', 'payload_refs', 'digest', 'total_size',
                     'delivery_mode', 'deposited_at'),
                    (*row[:6], loads(row[6]), loads(row[7]), *row[8:])))


def install(app):
    op, finish = registration(app, 'store', ('content',))
    listing_fields = {
        'name': {'type':'string','minLength':1,'maxLength':120},
        'mode': {'enum': ['sale', 'bounty']},
        'item_kind': {'enum': list(KINDS)},
        'price_minor': INTEGER,
        'currency_id': IDENTIFIER,
        'quantity': INTEGER,
        'delivery_mode': {'enum': list(MODES)},
        'package_ref': {'type':['string','null']},
        'escrow_policy': {'type':'string','minLength':1,'maxLength':80},
        'dispute_policy': {'type':'string','minLength':1,'maxLength':80},
        'terms': {'type':'string','minLength':1,'maxLength':16384},
        'expires_at': {'type':['string','null']},
        'state': {'enum': list(STATES)},
    }
    creation_required = ('name', 'item_kind', 'price_minor', 'currency_id', 'quantity',
                         'delivery_mode', 'escrow_policy', 'dispute_policy', 'terms')

    create_fields = {key:value for key,value in listing_fields.items()
                     if key not in {'state','package_ref'}}
    @op('store.listing_create', obj(create_fields, creation_required), signature=True)
    async def listing_create(ctx, request, tx):
        seller = _subject(ctx)
        await app.authorizer.require_base(ctx.principal, operation_id(request), seller, tx)
        await check_access(app, ctx, request, tx, 't_store', 'create')
        args = request.arguments
        body = {key: args.get(key) for key in create_fields if key != 'name'}
        body['mode'] = args.get('mode', 'sale')
        body['package_ref'] = None
        body['expires_at'] = args.get('expires_at')
        body['state'] = 'draft'
        _validate(body, ctx.now)
        resource = await create_resource(app, ctx, request, tx, parent='t_store',
            type='listing', name=args['name'], body=canonical(body),
            media_type='application/json', mode=0o644)
        require(resource.owner == seller, 'seller_mismatch')
        return HandlerOutput(resources=(ResourceRef(id=resource.id, revision=resource.revision),),
                             data={'listing': _public(resource, body),
                                   'generation': resource.generation})

    @op('store.listing_update', obj({'id':IDENTIFIER, 'price_minor':INTEGER,
        'quantity':INTEGER, 'terms':listing_fields['terms'], 'state':listing_fields['state'],
        'expires_at':listing_fields['expires_at'], 'package_ref':listing_fields['package_ref']},
        ('id',)), signature=True)
    async def listing_update(ctx, request, tx):
        resource = await _listing(app, ctx, request, tx, request.arguments['id'], seller=True)
        await check_access(app, ctx, request, tx, resource.id, 'write')
        await assert_generation(request, resource)
        old, _ = await _body(app, tx, resource)
        require(old['mode'] == 'sale', 'listing_mode_unsupported')
        updates = {key:value for key,value in request.arguments.items() if key != 'id'}
        require(bool(updates), 'empty_listing_update')
        body = {**old, **updates}
        require(old['state'] != 'closed' or body['state'] == 'closed', 'listing_closed')
        if body['package_ref'] is not None:
            row = await _package_row(tx, body['package_ref'])
            require(row is not None and row[1] == resource.id and row[3] == resource.owner and
                    row[5] == body['item_kind'] and row[10] == body['delivery_mode'],
                    'package_not_found')
        _validate(body, ctx.now)
        updated = await revise_resource(app, ctx, request, tx, resource, canonical(body),
                                        'application/json')
        return HandlerOutput(resources=(ResourceRef(id=updated.id, revision=updated.revision),),
                             data={'listing': _public(updated, body),
                                   'generation':updated.generation})

    @op('store.listing_get', obj({'id':IDENTIFIER, 'revision':IDENTIFIER}, ('id',)),
        effect='read')
    async def listing_get(ctx, request, tx):
        resource = await _listing(app, ctx, request, tx, request.arguments['id'])
        revision = request.arguments.get('revision')
        body, rev = await _body(app, tx, resource, revision)
        result = _public(resource, body)
        if body['mode'] == 'bounty':
            from msg.plugins.bounty import projection
            live = projection(tx,resource.id,ctx.now)
            result['current_state'] = live['state']
            result['pause_reason'] = live['pause_reason']
            result['current_budget_minor'] = live['budget_minor']
            result['escrow_balance_minor'] = live['escrow_balance_minor']
            result['paid_claims'] = live['paid_claims']
            if revision is None:
                result['state'] = live['state']
                result['budget_minor'] = live['budget_minor']
        result['listing_revision'] = rev.id
        result['terms_revision'] = rev.id
        return HandlerOutput(resources=(ResourceRef(id=resource.id, revision=rev.id),),
                             data={'listing':result})

    @op('store.package_deposit', obj({'listing_id':IDENTIFIER,
        'listing_revision':IDENTIFIER,
        'manifest':{'type':'object'},
        'payload_refs':{'type':'array','items':REF,'minItems':1,'maxItems':MAX_REFS}},
        ('listing_id','listing_revision','manifest','payload_refs')), signature=True)
    async def package_deposit(ctx, request, tx):
        from msg.plugins.communication import direct_ancestor
        resource = await _listing(app, ctx, request, tx, request.arguments['listing_id'], seller=True)
        body, _ = await _body(app, tx, resource)
        require(body['mode'] == 'sale', 'listing_mode_unsupported')
        require(resource.revision == request.arguments['listing_revision'],
                'listing_revision_conflict')
        require(body['delivery_mode'] == 'managed_instant' and
                body['item_kind'] in {'file','bundle'} and body['state'] != 'closed',
                'package_mode_unsupported')
        manifest = request.arguments['manifest']
        require(len(canonical(manifest)) <= MAX_MANIFEST_BYTES, 'manifest_too_large')
        refs=[]
        blobs=[]
        total=0
        for ref in request.arguments['payload_refs']:
            require(ref['revision'] is not None, 'payload_revision_required')
            rid=await resolve(tx,ref['id'])
            item=await tx.resource(rid)
            require(item.owner == resource.owner and item.state == 'active' and
                    item.type in {'file','attachment'}, 'payload_not_owned')
            require(await direct_ancestor(tx, rid) is None, 'payload_private_conversation')
            await check_access(app, ctx, request, tx, rid, 'read')
            revision=await tx.revision(ResourceRef(id=rid,revision=ref['revision']))
            blobs.append(revision.content)
            total+=revision.content.size
            require(total <= 1024**4, 'package_too_large')
            refs.append({'id':rid,'revision':revision.id,'blob':wire(revision.content)})
        require(len({(r['id'],r['revision']) for r in refs}) == len(refs),
                'duplicate_payload_ref')
        require(body['item_kind'] == 'bundle' or len(refs) == 1,
                'file_package_single_payload')
        package_id=new_id('pkg')
        package_revision=new_id('pv')
        snapshot={'seller':resource.owner,'listing_id':resource.id,
                  'listing_revision':resource.revision,'revision':package_revision,
                  'kind':body['item_kind'],'manifest':manifest,'payload_refs':refs,
                  'total_size':total,'delivery_mode':'managed_instant'}
        package_digest=digest(snapshot)
        for blob in blobs:
            await app.contents.pin(blob, package_revision)
        tx.execute('''INSERT INTO store_packages
            (id,listing_id,listing_revision,seller,revision,kind,manifest,payload_refs,
             digest,total_size,delivery_mode,deposited_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (package_id,resource.id,resource.revision,resource.owner,package_revision,
             body['item_kind'],canonical(manifest).decode(),canonical(refs).decode(),
             package_digest,total,'managed_instant',wire(ctx.now)),write=True)
        return HandlerOutput(resources=(ResourceRef(id=resource.id,revision=resource.revision),),
                             data={'package':{'id':package_id,'revision':package_revision,
                                              'digest':package_digest,'total_size':total}})

    @op('store.package_get', obj({'id':IDENTIFIER}, ('id',)), effect='read')
    async def package_get(ctx, request, tx):
        row=await _package_row(tx,request.arguments['id'])
        require(row is not None and row[3] == _subject(ctx), 'package_not_found')
        await _listing(app, ctx, request, tx, row[1], seller=True)
        return HandlerOutput(data={'package':_package_public(row)})

    finish((ResourceTypeSpec(name='listing',version=1,container=False,
        content_schema=None,operations=frozenset(),relations=frozenset()),))
