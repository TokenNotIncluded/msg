"""Controlled Order Resources; SQL rows are checked projections of revisions.

Only explicit market operation boundaries may advance the source. A database
projection mismatch is an error, never input for an automatic repair/re-sign.
"""
from dataclasses import replace
from contextlib import asynccontextmanager
from functools import wraps
from uuid import uuid4

from msg.core.codec import canonical, decode, digest, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import Resource, ResourceRef, Revision
from msg.security.crypto import verify


def _active(tx):
    if not hasattr(tx, '_order_resource_mutations'):
        tx._order_resource_mutations = set()
    return tx._order_resource_mutations


def begin_new(tx, order_id):
    _active(tx).add(order_id)


def snapshot(tx, order_id):
    from msg.market.order_records import _COLUMNS
    row = tx.one('SELECT ' + ','.join(_COLUMNS) + ' FROM store_orders WHERE id=?', (order_id,))
    require(row is not None, 'order_resource_missing')
    order = dict(zip(_COLUMNS, row))
    # Notification endpoint/generation/jobs have their own authority and may
    # change without changing ownership, price, delivery or acceptance.
    order.pop('delivery_target')
    order['receipt_refs'] = loads(order['receipt_refs'])
    locked = tx.one('SELECT body,digest FROM order_contracts WHERE order_id=?', (order_id,))
    require(locked is not None and digest(loads(locked[0])) == locked[1], 'order_contract_corrupt')
    fact = tx.one('SELECT body FROM order_settlements WHERE order_id=?', (order_id,))
    return {'model': 1, 'order': order, 'contract': loads(locked[0]),
            'settlement': loads(fact[0]) if fact else None,
            'transitions': [loads(row[0]) for row in tx.rows(
                'SELECT body FROM order_transitions WHERE order_id=? ORDER BY id', (order_id,))]}


def source_metadata(tx, order_id, *, force=False):
    contract_row = tx.one('SELECT body FROM order_contracts WHERE order_id=?', (order_id,))
    if contract_row is None or loads(contract_row[0]).get('resource_model') != 1:
        require(tx.one("SELECT 1 FROM resources WHERE id=? AND type='order'", (order_id,)) is None,
                'order_resource_contract_missing')
        return None  # Published historical writer; no implicit adoption.
    if not force and order_id in _active(tx):
        return None  # Validated before this dedicated operation began.
    row = tx.one('SELECT body FROM resources WHERE id=?', (order_id,))
    require(row is not None, 'order_resource_missing')
    resource = decode(Resource, loads(row[0]))
    buyer = loads(contract_row[0])['buyer']
    require(resource.type == 'order' and resource.type_version == 1 and resource.owner == buyer and
            resource.name == order_id and resource.parent == 'orders_' + buyer and
            resource.mode == 0o400 and resource.state == 'active', 'order_resource_invalid')
    row = tx.one('SELECT body FROM revisions WHERE id=? AND resource_id=?', (resource.revision, order_id))
    require(row is not None, 'order_resource_missing')
    revision = decode(Revision, loads(row[0]))
    manifest = {k: v for k, v in wire(revision).items() if k not in {'manifest_digest', 'signature'}}
    require(digest(manifest) == revision.manifest_digest and revision.signature is not None and
            revision.content.digest == digest(snapshot(tx, order_id)), 'order_resource_projection_mismatch')
    return resource, revision


async def verify_source(app, tx, order_id):
    source = source_metadata(tx, order_id, force=True)
    if source is None:
        return None
    resource, revision = source
    manifest = {k: v for k, v in wire(revision).items() if k not in {'manifest_digest', 'signature'}}
    verify(app.receipt_signer.public_key, canonical(manifest), revision.signature, purpose='revision')
    try:
        body = await app.contents.read_bytes(revision.content)
    except FileNotFoundError as exc:
        raise Failure('content_missing') from exc
    require(body == canonical(snapshot(tx, order_id)), 'order_resource_projection_mismatch')
    return resource


async def publish(app, tx, order_id, *, now, actor, request_id, request_digest, version):
    require(order_id in _active(tx), 'order_resource_write_forbidden')
    app.registry.resource_type('order', 1)
    app.registry.resource_type('order_collection', 1)
    from msg.market.policy import contract
    locked = contract(tx, order_id)  # All existing finance/acceptance checks still apply.
    if locked.get('resource_model') != 1:
        return None
    body = snapshot(tx, order_id)
    row = tx.one('SELECT body FROM resources WHERE id=?', (order_id,))
    buyer = locked['buyer']
    if row is None:
        parent_id = 'orders_' + buyer
        parent_row = tx.one('SELECT body FROM resources WHERE id=?', (parent_id,))
        if parent_row is None:
            require(tx.one('SELECT 1 FROM resources WHERE parent=? AND name=?', (buyer, 'orders')) is None,
                    'order_collection_conflict')
            owner = await tx.subject(buyer)
            parent = Resource(id=parent_id, type='order_collection', type_version=1, name='orders',
                parent=buyer, owner=buyer, group=owner.primary_group, mode=0o500, generation=0,
                revision=None, state='active', created_at=now, created_by=actor, modified_at=now, modified_by=actor)
            await tx.insert(parent)
        else:
            parent = decode(Resource, loads(parent_row[0]))
            require(parent.type == 'order_collection' and parent.parent == buyer and parent.owner == buyer and
                    parent.mode == 0o500 and parent.state == 'active', 'order_collection_invalid')
        resource = Resource(id=order_id, type='order', type_version=1, name=order_id,
            parent=parent_id, owner=buyer, group=parent.group, mode=0o400, generation=0,
            revision=None, state='active', created_at=parse_time(locked['created_at']), created_by=buyer,
            modified_at=now, modified_by=actor)
        await tx.insert(resource)
    else:
        resource = decode(Resource, loads(row[0]))
        current = await tx.revision(ResourceRef(id=order_id))
        if current.content.digest == digest(body):
            return ResourceRef(id=order_id, revision=resource.revision)
    blob = await app.contents.put_bytes(canonical(body), 'application/json')
    rid = 'v_' + uuid4().hex
    revision = Revision(format_version=1, id=rid, resource_id=order_id,
        parents=(resource.revision,) if resource.revision else (), content=blob, relations=(),
        actor=actor, subject=actor, author=actor, created_at=now, manifest_digest='',
        source_kind='operation', source_version=version,
        source_digest=request_digest or digest({'request_id': request_id, 'order_id': order_id}))
    manifest = {k: v for k, v in wire(revision).items() if k not in {'manifest_digest', 'signature'}}
    revision = replace(revision, manifest_digest=digest(manifest),
                       signature=app.receipt_signer.sign(canonical(manifest), purpose='revision'))
    tx.on_rollback(lambda: app.contents.unpin(blob, rid))
    await app.contents.pin(blob, rid)
    await app.contents.commit_revision(resource.parent, revision)
    await tx.append_revision(revision)
    await tx.replace(replace(resource, revision=rid, generation=resource.generation + 1,
                             modified_at=now, modified_by=actor), resource.generation)
    return ResourceRef(id=order_id, revision=rid)


def operation_boundary(app, op):
    """Wrap only registered market operations, keeping core executor unchanged."""
    def register(name, schema, **options):
        def decorate(handler):
            @wraps(handler)
            async def bounded(ctx, request, tx):
                before = set(_active(tx))
                ids = set()
                args = request.arguments
                order_id = args.get('order_id')
                case_id = args.get('case_id') or args.get('proposal', {}).get('case_id')
                if case_id:
                    row = tx.one('SELECT order_id FROM arbitration_cases WHERE id=?', (case_id,))
                    order_id = row[0] if row else None
                if order_id:
                    # Authorization and not-found equivalence remain the handler's job.
                    row = tx.one('SELECT buyer,seller FROM store_orders WHERE id=?', (order_id,))
                    if row and (ctx.principal.subject in row or case_id):
                        await verify_source(app, tx, order_id)
                        ids.add(order_id)
                writing = options.get('effect', 'transaction') != 'read'
                if writing:
                    _active(tx).update(ids)
                try:
                    result = await handler(ctx, request, tx)
                    ids.update(_active(tx) - before)
                    data = result.data or {}
                    for item in data.get('orders', ()):
                        if item.get('source') != 'legacy_purchase':
                            ids.add(item['id'])
                    if writing:
                        refs = []
                        for oid in sorted(ids):
                            row = tx.one('SELECT body FROM order_contracts WHERE order_id=?', (oid,))
                            if row and loads(row[0]).get('resource_model') == 1:
                                ref = await publish(app, tx, oid, now=ctx.now, actor=ctx.principal.subject,
                                    request_id=request.request_id, request_digest=request.payload_digest,
                                    version=request.contract_version)
                                if loads(row[0])['buyer'] == ctx.principal.subject:
                                    refs.append(ref)
                        if refs:
                            # Existing account-scoped contracts do not grant generic
                            # Resource.read over descendants. Keep their replay ACL;
                            # expose references as data, without expanding authority.
                            result = replace(result, data={**data, 'order_resources': wire(tuple(refs))})
                    else:
                        for oid in ids:
                            await verify_source(app, tx, oid)
                    return result
                finally:
                    tx._order_resource_mutations = before
            return op(name, schema, **options)(bounded)
        return decorate
    return register


@asynccontextmanager
async def internal_mutation(app, tx, order_id, *, now, actor, request_id):
    """Timer/internal entrypoint, using the same verified source boundary."""
    if order_id in _active(tx):
        yield
        return
    before = set(_active(tx))
    await verify_source(app, tx, order_id)
    _active(tx).add(order_id)
    try:
        yield
        row = tx.one('SELECT body FROM order_contracts WHERE order_id=?', (order_id,))
        if row and loads(row[0]).get('resource_model') == 1:
            await publish(app, tx, order_id, now=now, actor=actor, request_id=request_id,
                          request_digest=None, version=1)
    finally:
        tx._order_resource_mutations = before
