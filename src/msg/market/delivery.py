"""Automatic managed packages and explicitly accepted ciphertext/service evidence."""
from __future__ import annotations

import hashlib

from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import BlobRef, HandlerOutput, ResourceRef
from msg.market.escrow import settle, transition
from msg.market.orders import HASH, ORDER
from msg.market.policy import contract
from msg.market.targets import enqueue_notification, validate_target
from msg.plugins.common import check_access, new_id
from msg.plugins.delivery import _buyer_order, _delivery
from msg.plugins.orders import _row, _subject, _viewer
from msg.plugins.schemas import IDENTIFIER, REF, obj
from msg.plugins.store import _package_row

INLINE = 65536
PART = 65536


async def verify_blob(app, blob):
    """Bounded-memory verification, including content loss after a deposit."""
    hasher, size = hashlib.sha256(), 0
    try:
        async for chunk in app.contents.read(blob):
            hasher.update(chunk)
            size += len(chunk)
    except FileNotFoundError:
        raise Failure('package_missing') from None
    except Failure as exc:
        if exc.code == 'content_missing':
            raise Failure('package_missing') from None
        if exc.code in {'content_truncated','content_size_mismatch'}:
            raise Failure('package_digest_mismatch') from None
        raise
    require(size == blob.size and 'sha256:'+hasher.hexdigest() == blob.digest,
            'package_digest_mismatch')


async def package(app, tx, order):
    row = await _package_row(tx, order['package_id'])
    require(row is not None, 'package_missing')
    require(row[1] == order['listing_id'] and row[3] == order['seller'] and
            row[4] == order['package_revision'] and row[8] == order['package_digest'] and
            row[10] == 'managed_instant' and row[5] in {'file','bundle','text'},
            'package_digest_mismatch')
    refs, manifest = loads(row[7]), loads(row[6])
    snapshot = {'seller': row[3], 'listing_id': row[1], 'listing_revision': row[2],
        'revision': row[4], 'kind': row[5], 'manifest': manifest, 'payload_refs': refs,
        'total_size': row[9], 'delivery_mode': row[10]}
    require(digest(snapshot) == order['package_digest'] and 0 <= row[9] <= 1024**4,
            'package_digest_mismatch')
    require(1 <= len(refs) <= 32, 'package_digest_mismatch')
    total = 0
    for ref in refs:
        blob = decode(BlobRef, ref['blob'])
        await verify_blob(app, blob)
        total += blob.size
    require(total == row[9], 'package_digest_mismatch')
    return row[5], manifest, refs


def body_for(order, delivery_id, kind, manifest, refs):
    return {'delivery_id': delivery_id, 'order_id': order['id'],
        'recipient_subject': order['buyer'], 'kind': kind, 'manifest': manifest,
        'payload_refs': refs, 'package_digest': order['package_digest'] or
            contract_digest(order), 'channel': 'site'}


def contract_digest(order):
    # Non-managed goods have no deposited package. This is a terms commitment,
    # not a false claim that the service/ciphertext content was fixed at checkout.
    return digest({'listing_revision': order['listing_revision'], 'terms_digest': order['terms_digest']})


async def prepare(app, tx, ctx, request, order, kind, manifest, refs, *, envelope=None):
    require(order['state'] == 'funded' and order['delivered_at'] is None,
            'order_not_deliverable')
    validate_target(tx, order, encryption=envelope is not None)
    delivery_id = new_id('dlv')
    for ref in refs:
        await app.contents.pin(decode(BlobRef, ref['blob']), delivery_id)
    body = body_for(order, delivery_id, kind, manifest, refs)
    tx.execute('''INSERT INTO store_deliveries
        (id,order_id,recipient_subject,kind,payload_refs,manifest,package_digest,
         delivery_digest,channel,state,prepared_at,claimed_at,receipt)
        VALUES (?,?,?,?,?,?,?,?,'site','prepared',?,NULL,NULL)''',
        (delivery_id,order['id'],order['buyer'],kind,canonical(refs).decode(),
         canonical(manifest).decode(),body['package_digest'],digest(body),wire(ctx.now)), write=True)
    if envelope:
        tx.execute('INSERT INTO delivery_envelopes(order_id,body) VALUES (?,?)',
                   (order['id'],canonical(envelope).decode()), write=True)
    await transition(tx, order, 'delivered', now=ctx.now, actor=ctx.principal.actor,
                     request_id=request.request_id, reason='verified_delivery')
    tx.execute('UPDATE store_orders SET delivered_at=? WHERE id=?',
               (wire(ctx.now),order['id']), write=True)
    order['delivered_at'] = wire(ctx.now)
    # Inbox is a minimal reference to the one authoritative order, not a copy.
    message = {'id': new_id('message'), 'sender': order['seller'], 'actor': order['seller'],
        'recipient': order['buyer'], 'order_id': order['id'], 'time': wire(ctx.now),
        'resource': wire(ResourceRef(id=order['buyer'])), 'state': 'delivered', 'source': 'system',
        'href': '/_orders/'+order['id']+'/_delivery'}
    tx.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
        (message['id'],order['seller'],order['buyer'],order['buyer'],
         'delivery:'+order['id'],canonical(message).decode()), write=True)
    await enqueue_notification(app, tx, ctx, request, order)
    return _delivery(tx, order['id'])


async def automatic(app, tx, ctx, request, order):
    """An authenticated payment authorizes the pinned managed-instant policy.

    Only verified objective storage faults refund. An arbitrary exception rolls
    back the executor transaction, rather than turning software bugs into payouts.
    """
    try:
        kind, manifest, refs = await package(app, tx, order)
    except Failure as exc:
        if exc.code not in {'package_missing','package_digest_mismatch'}:
            raise
        await settle(app, tx, order, now=ctx.now, actor=order['buyer'],
            request_id=request.request_id, reason=exc.code, refund_minor=order['total_price_minor'])
        return
    await prepare(app, tx, ctx, request, order, kind, manifest, refs)
    await transition(tx, order, 'accepted', now=ctx.now, actor=order['buyer'],
                     request_id=request.request_id, reason='managed_instant_verified')
    await settle(app, tx, order, now=ctx.now, actor=order['buyer'],
                 request_id=request.request_id, reason='managed_instant_verified')
    # Delivery remains prepared, not claimed. Server fulfilment is not buyer ACK.


async def verified(app, tx, order, *, verify_bytes=True):
    delivery = _delivery(tx, order['id'])
    require(delivery is not None, 'delivery_not_found')
    validate_target(tx, order)
    require(delivery['recipient_subject'] == order['buyer'], 'delivery_recipient_mismatch')
    expected = body_for(order, delivery['id'], delivery['kind'],
                        delivery['manifest'], delivery['payload_refs'])
    require(delivery['package_digest'] == expected['package_digest'] and
            delivery['delivery_digest'] == digest(expected), 'delivery_digest_mismatch')
    locked = contract(tx, order['id'])
    if locked['listing']['delivery_mode'] == 'managed_instant':
        row = await _package_row(tx, order['package_id'])
        require(row is not None and row[8] == order['package_digest'] and
                loads(row[6]) == delivery['manifest'] and loads(row[7]) == delivery['payload_refs'],
                'delivery_digest_mismatch')
    elif locked['listing']['delivery_mode'] == 'sealed_manual':
        envelope = tx.one('SELECT body FROM delivery_envelopes WHERE order_id=?', (order['id'],))
        require(envelope is not None, 'delivery_digest_mismatch')
        envelope = loads(envelope[0])
        require(envelope['recipient_subject'] == order['buyer'] and
                envelope['recipient_key'] == locked['recipient_key'] and
                envelope['ciphertext_ref'] == delivery['payload_refs'][0],
                'delivery_recipient_mismatch')
    if verify_bytes:
        for ref in delivery['payload_refs']:
            await verify_blob(app, decode(BlobRef, ref['blob']))
    return delivery


async def read(app, tx, ctx, request):
    buyer = _viewer(ctx)
    order = _buyer_order(tx, request.arguments['order_id'], buyer)
    delivery = await verified(app, tx, order)
    payloads, budget = [], INLINE
    for index, ref in enumerate(delivery['payload_refs']):
        blob = decode(BlobRef, ref['blob'])
        item = {'id': ref['id'], 'revision': ref['revision'], 'digest': blob.digest,
                'size': blob.size, 'media_type': blob.media_type, 'payload_index': index}
        if blob.size <= budget:
            item['data'] = b64(await app.contents.read_bytes(blob, limit=INLINE))
            budget -= blob.size
        else:
            item['transfer'] = {'operation':'delivery.transfer_open','arguments':{
                'order_id':order['id'],'delivery_digest':delivery['delivery_digest'],
                'payload_index':index}}
            item['next'] = {'operation': 'delivery.part_get', 'arguments': {
                'order_id': order['id'], 'delivery_digest': delivery['delivery_digest'],
                'payload_index': index, 'offset': 0, 'length': min(PART,blob.size)}}
        payloads.append(item)
    result = {k:v for k,v in delivery.items() if k not in {'id','payload_refs','receipt'}}
    result.update(delivery_id=delivery['id'], payloads=payloads)
    return HandlerOutput(data={'delivery': result})


async def accept(app, tx, ctx, request):
    buyer = _subject(ctx)
    order = _buyer_order(tx, request.arguments['order_id'], buyer)
    delivery = await verified(app, tx, order)
    require(delivery['delivery_digest'] == request.arguments['delivery_digest'], 'delivery_mismatch')
    require(order['state'] in {'delivered','settled'}, 'delivery_not_acceptable')
    if delivery['state'] == 'claimed':
        return HandlerOutput(data={'order_id': order['id'], 'state': order['state'],
            'delivery_id': delivery['id'], 'receipt': delivery['receipt']})
    settlement = []
    if order['state'] == 'delivered':
        await transition(tx, order, 'accepted', now=ctx.now, actor=buyer,
                         request_id=request.request_id, reason='buyer_acceptance')
        settlement = await settle(app, tx, order, now=ctx.now, actor=buyer,
                                  request_id=request.request_id, reason='buyer_acceptance')
    body = {'order_id': order['id'], 'delivery_id': delivery['id'], 'subject': buyer,
        'delivery_digest': delivery['delivery_digest'], 'request_digest': request.payload_digest,
        'claimed_at': wire(ctx.now)}
    receipt = {'body': body,
               'signature': wire(app.receipt_signer.sign(canonical(body), purpose='delivery-ack'))}
    tx.execute("UPDATE store_deliveries SET state='claimed',claimed_at=?,receipt=? WHERE order_id=?",
        (wire(ctx.now),canonical(receipt).decode(),order['id']), write=True)
    return HandlerOutput(data={'order_id': order['id'], 'state': order['state'],
        'delivery_id': delivery['id'], 'receipt': receipt, 'settlement_receipts': settlement})


def install(app, op):
    @op('delivery.get', ORDER, effect='read', version=2)
    async def get(ctx, request, tx):
        return await read(app, tx, ctx, request)

    @op('delivery.accept', obj({'order_id': IDENTIFIER, 'delivery_digest': HASH},
        ('order_id','delivery_digest')), signature=True, version=2)
    async def claim(ctx, request, tx):
        return await accept(app, tx, ctx, request)

    @op('delivery.prepare', ORDER, signature=True, version=2)
    async def prepare_managed(ctx, request, tx):
        buyer = _subject(ctx)
        order = _buyer_order(tx, request.arguments['order_id'], buyer)
        locked = contract(tx, order['id'])
        require(locked['listing']['delivery_mode'] == 'managed_instant', 'seller_delivery_required')
        if order['state'] == 'funded':
            await automatic(app, tx, ctx, request, order)
        return HandlerOutput(data={'order_id': order['id'], 'state': order['state']})

    @op('delivery.submit', obj({'order_id': IDENTIFIER, 'payload_ref': REF,
        'payload_digest': HASH, 'recipient_key_id': IDENTIFIER},
        ('order_id','payload_ref','payload_digest')), signature=True)
    async def submit(ctx, request, tx):
        from msg.plugins.communication import direct_ancestor
        seller = _subject(ctx)
        order = _row(tx, request.arguments['order_id'], seller)
        require(order['seller'] == seller, 'order_not_found')
        locked = contract(tx, order['id'])
        mode = locked['listing']['delivery_mode']
        require(mode in {'sealed_manual','service'}, 'order_delivery_mode_unsupported')
        require(order['state'] == 'funded', 'order_not_deliverable')
        from datetime import timedelta

        from msg.core.codec import parse_time
        require(ctx.now < parse_time(order['funded_at']) + timedelta(
            seconds=locked['policy']['policy']['delivery_timeout_seconds']), 'delivery_deadline_passed')
        ref = decode(ResourceRef, request.arguments['payload_ref'])
        require(ref.revision is not None, 'payload_revision_required')
        resource = await tx.resource(ref.id)
        require(resource.owner == seller and resource.type in {'file','attachment'} and
                resource.state == 'active', 'payload_not_owned')
        require(await direct_ancestor(tx, ref.id) is None, 'payload_private_conversation')
        await check_access(app, ctx, request, tx, ref.id, 'read')
        revision = await tx.revision(ref)
        blob = revision.content
        require(blob.digest == request.arguments['payload_digest'], 'delivery_digest_mismatch')
        await verify_blob(app, blob)
        payload = {'id': ref.id, 'revision': ref.revision, 'blob': wire(blob)}
        envelope = None
        if mode == 'sealed_manual':
            validate_target(tx, order, encryption=True)
            require(request.arguments.get('recipient_key_id') == locked['recipient_key']['key_id'],
                    'delivery_recipient_mismatch')
            prefix = b''.join([piece async for piece in app.contents.read(blob, (0,min(64,blob.size)))])
            require(blob.media_type in {'application/age','application/octet-stream'} and
                    prefix.startswith(b'age-encryption.org/v1\n'), 'encrypted_delivery_required')
            envelope = {'recipient_subject': order['buyer'], 'recipient_key': locked['recipient_key'],
                        'ciphertext_ref': payload, 'seller_request_digest': request.payload_digest}
            manifest = {'encryption': 'age-x25519', 'recipient_key_id': locked['recipient_key']['key_id']}
        else:
            require('recipient_key_id' not in request.arguments, 'unexpected_recipient_key')
            manifest = {'type': 'service-evidence', 'acceptance': 'explicit-buyer-signature'}
        delivery = await prepare(app, tx, ctx, request, order, locked['listing']['item_kind'],
                                 manifest, [payload], envelope=envelope)
        return HandlerOutput(data={'order_id': order['id'], 'delivery_id': delivery['id'],
            'delivery_digest': delivery['delivery_digest'], 'state': 'delivered'})

    @op('delivery.part_get', obj({'order_id': IDENTIFIER, 'delivery_digest': HASH,
        'payload_index': {'type': 'integer','minimum': 0,'maximum': 31},
        'offset': {'type': 'integer','minimum': 0,'maximum': 1024**4},
        'length': {'type': 'integer','minimum': 1,'maximum': PART}},
        ('order_id','delivery_digest','payload_index','offset','length')), effect='read')
    async def part_get(ctx, request, tx):
        a = request.arguments
        order = _buyer_order(tx, a['order_id'], _viewer(ctx))
        delivery = await verified(app, tx, order, verify_bytes=False)
        require(a['delivery_digest'] == delivery['delivery_digest'], 'delivery_digest_mismatch')
        require(a['payload_index'] < len(delivery['payload_refs']), 'payload_not_found')
        blob = decode(BlobRef, delivery['payload_refs'][a['payload_index']]['blob'])
        start, end = a['offset'], a['offset']+a['length']
        require(start < end <= blob.size, 'invalid_byte_range')
        data = b''.join([piece async for piece in app.contents.read(blob, (start,end))])
        require(len(data) == a['length'], 'delivery_digest_mismatch')
        following = ({**a, 'offset': end, 'length': min(PART,blob.size-end)} if end<blob.size else None)
        return HandlerOutput(data={'order_id': order['id'], 'delivery_id': delivery['id'],
            'delivery_digest': delivery['delivery_digest'], 'payload_digest': blob.digest,
            'offset': start, 'size': blob.size, 'data': b64(data), 'chunk_digest': digest(data),
            'next': following})

    @op('delivery.transfer_open', obj({'order_id':IDENTIFIER,'delivery_digest':HASH,
        'payload_index':{'type':'integer','minimum':0,'maximum':31}},
        ('order_id','delivery_digest','payload_index')),signature=True)
    async def transfer_open(ctx, request, tx):
        from datetime import timedelta

        from msg.core.transfer import TransferService
        from msg.plugins.common import create_resource
        from msg.plugins.transfer import negotiate
        a = request.arguments
        buyer = _subject(ctx)
        order = _buyer_order(tx,a['order_id'],buyer)
        delivery = await verified(app,tx,order)
        require(a['delivery_digest']==delivery['delivery_digest'],'delivery_digest_mismatch')
        require(a['payload_index']<len(delivery['payload_refs']),'payload_not_found')
        blob = decode(BlobRef,delivery['payload_refs'][a['payload_index']]['blob'])
        parent = tx.one("SELECT id FROM resources WHERE parent=? AND name='files'",(buyer,))
        require(parent is not None,'upload_parent_required')
        await check_access(app,ctx,request,tx,parent[0],'create')
        # A purchased copy has independent buyer ACL, but references the exact
        # existing BlobRef. No duplicate payload bytes and no seller ACL bypass
        # through the generic file API. Only this signed operation can publish it.
        resource = await create_resource(app,ctx,request,tx,parent=parent[0],type='file',
            body=blob,media_type=blob.media_type,mode=0o600)
        async def no_upload(*args):
            raise Failure('wrong_transfer_direction')
        limits = negotiate(app,request)
        service = TransferService(app.metadata,app.contents,app.authorizer,app.clock,
            publish=no_upload,ttl=app.settings.transfer_ttl,part_bytes=app.settings.max_part_bytes)
        session = await service.open(ctx,'download',ResourceRef(id=resource.id,revision=resource.revision),
            blob.size,blob.digest,ctx.now+timedelta(seconds=app.settings.transfer_ttl),limits=limits)
        return HandlerOutput(resources=(ResourceRef(id=resource.id,revision=resource.revision),),
            data={'transfer_id':session.id,'order_id':order['id'],'delivery_id':delivery['id'],
                  'delivery_digest':delivery['delivery_digest'],'size':blob.size,'digest':blob.digest,
                  'state':session.state,'expires_at':wire(session.expires_at),**limits})
