"""Private, immutable deliveries with finite automatic or explicit acceptance.

Sealed-manual ciphertext and service completion are seller assertions. The server
cannot verify plaintext or service quality; only buyer acceptance settles them.
"""
from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import BlobRef, HandlerOutput, ResourceRef
from msg.plugins.common import check_access, new_id, registration, resolve
from msg.plugins.money import account_requirements, CURRENCY_ID, _balance, _post_transfer
from msg.plugins.orders import _row as order_row, _subject
from msg.plugins import order_engine as engine
from msg.plugins.schemas import IDENTIFIER, REF, obj
from msg.plugins.store import _package_row

MAX_INLINE_BYTES = 1024 * 1024
_COLUMNS = ('id','order_id','recipient_subject','kind','payload_refs','manifest',
            'package_digest','delivery_digest','channel','state','prepared_at','claimed_at','receipt')


def _delivery(tx,order_id):
    row=tx.one('''SELECT id,order_id,recipient_subject,kind,payload_refs,manifest,package_digest,
        delivery_digest,channel,state,prepared_at,claimed_at,receipt FROM store_deliveries WHERE order_id=?''',(order_id,))
    if row is None:return None
    result=dict(zip(_COLUMNS,row))
    result['payload_refs']=loads(result['payload_refs'])
    result['manifest']=loads(result['manifest'])
    result['receipt']=loads(result['receipt']) if result['receipt'] else None
    return result


def _buyer_order(tx,order_id,buyer):
    row=order_row(tx,order_id,buyer)
    require(row['buyer']==buyer,'order_not_found')
    return row


def _delivery_body(order,delivery_id,kind,manifest,refs):
    return {'delivery_id':delivery_id,'order_id':order['id'],'recipient_subject':order['buyer'],
            'kind':kind,'manifest':manifest,'payload_refs':refs,
            'package_digest':order['package_digest'],'channel':'site'}


async def _verified_delivery(app,tx,order,delivery):
    require(delivery is not None and delivery['recipient_subject']==order['buyer'] and
            delivery['package_digest']==order['package_digest'] and delivery['channel']=='site',
            'delivery_mismatch')
    kind,manifest,refs=delivery['kind'],delivery['manifest'],delivery['payload_refs']
    if order['delivery_mode']=='managed_instant':
        expected_kind,expected_manifest,expected_refs=await _package(app,tx,order)
        require(kind==expected_kind and manifest==expected_manifest and refs==expected_refs,'delivery_mismatch')
    elif order['delivery_mode']=='sealed_manual':
        require(kind=='sealed' and len(refs)==1 and isinstance(manifest,dict) and
                manifest.get('verification')=='buyer_decrypts','delivery_mismatch')
        key=tx.one('SELECT subject,recipient FROM encryption_subkeys WHERE key_id=?',
                   (manifest.get('recipient_key_id'),))
        require(key is not None and key==(order['buyer'],manifest.get('recipient')),'delivery_mismatch')
        await _payload(app,delivery)
    else:
        require(order['delivery_mode']=='service' and kind=='service' and refs==[] and
                isinstance(manifest,dict) and isinstance(manifest.get('completion_statement'),str) and
                manifest.get('verification')=='buyer_accepts_service','delivery_mismatch')
    require(delivery['delivery_digest']==digest(_delivery_body(order,delivery['id'],kind,manifest,refs)),
            'delivery_mismatch')
async def _package(app, tx, order):
    require(order['package_id'] and order['package_revision'] and
            order['package_digest'], 'delivery_package_mismatch')
    row = await _package_row(tx, order['package_id'])
    require(row is not None and row[1] == order['listing_id'] and
            row[3] == order['seller'] and row[4] == order['package_revision'] and
            row[8] == order['package_digest'] and row[10] == 'managed_instant' and
            row[5] in {'file', 'bundle'}, 'delivery_package_mismatch')
    manifest, refs = loads(row[6]), loads(row[7])
    snapshot = {'seller': row[3], 'listing_id': row[1],
                'listing_revision': row[2], 'revision': row[4], 'kind': row[5],
                'manifest': manifest, 'payload_refs': refs,
                'total_size': row[9], 'delivery_mode': row[10]}
    require(digest(snapshot) == order['package_digest'] and
            row[9] <= MAX_INLINE_BYTES and row[9] >= 0,
            'delivery_package_mismatch')
    require(refs and (row[5] == 'bundle' or len(refs) == 1),
            'delivery_package_mismatch')
    total = 0
    for ref in refs:
        require(isinstance(ref, dict) and isinstance(ref.get('blob'), dict),
                'delivery_package_mismatch')
        blob = decode(BlobRef, ref['blob'])
        require(blob.size >= 0 and blob.size <= MAX_INLINE_BYTES,
                'delivery_package_mismatch')
        data = await app.contents.read_bytes(blob, limit=MAX_INLINE_BYTES)
        require(len(data) == blob.size and digest(data) == blob.digest,
                'delivery_package_mismatch')
        total += blob.size
    require(total == row[9], 'delivery_package_mismatch')
    return row[5], manifest, refs


async def _payload(app, delivery):
    files = []
    for ref in delivery['payload_refs']:
        blob = decode(BlobRef, ref['blob'])
        data = await app.contents.read_bytes(blob, limit=MAX_INLINE_BYTES)
        require(len(data) == blob.size and digest(data) == blob.digest,
                'delivery_package_mismatch')
        files.append({'id': ref['id'], 'revision': ref['revision'],
                      'media_type': blob.media_type, 'digest': blob.digest,
                      'size': blob.size, 'data': b64(data)})
    return files


def _notice(tx,order,ctx,request,delivery_id):
    from msg.plugins.communication import event_id
    notice={'id':new_id('message'),'sender':None,'actor':ctx.principal.actor,
        'recipient':order['buyer'],'resource':wire(ResourceRef(id=order['listing_id'])),
        'time':wire(ctx.now),'state':'delivered','source':'store_delivery',
        'order_id':order['id'],'delivery_id':delivery_id}
    tx.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
        (notice['id'],None,order['buyer'],order['listing_id'],event_id(request,ctx.principal.actor),
         canonical(notice).decode()),write=True)


def _insert_delivery(tx,order,ctx,request,kind,manifest,refs):
    require(order['state']=='funded' and order['delivered_at'] is None,'order_not_deliverable')
    require(order['delivery_target']=={'subject_id':order['buyer'],'channel':'site'},'delivery_recipient_mismatch')
    require(_balance(tx,order['escrow_subject'])==order['total_price_minor'],'escrow_balance_mismatch')
    delivery_id=new_id('dlv')
    body=_delivery_body(order,delivery_id,kind,manifest,refs)
    delivery_digest=digest(body)
    tx.execute('''INSERT INTO store_deliveries
        (id,order_id,recipient_subject,kind,payload_refs,manifest,package_digest,
         delivery_digest,channel,state,prepared_at,claimed_at,receipt)
        VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,NULL,NULL)''',
        (delivery_id,order['id'],order['buyer'],kind,canonical(refs).decode(),canonical(manifest).decode(),
         order['package_digest'],delivery_digest,'site',wire(ctx.now)),write=True)
    tx.execute('UPDATE store_orders SET delivered_at=? WHERE id=?',(wire(ctx.now),order['id']),write=True)
    engine.transition(tx,order,'delivered',ctx,request,'delivery_recorded')
    _notice(tx,order,ctx,request,delivery_id)
    return {'delivery_id':delivery_id,'order_id':order['id'],'state':'prepared','channel':'site',
            'package_digest':order['package_digest'],'delivery_digest':delivery_digest}


async def prepare_order(app,tx,order,ctx,request):
    require(order['state']=='funded' and order['delivered_at'] is None,'order_not_deliverable')
    require(order['delivery_mode']=='managed_instant','order_delivery_mode_unsupported')
    require(order['delivery_target']=={'subject_id':order['buyer'],'channel':'site'},'delivery_recipient_mismatch')
    require(engine.deadline(order)>ctx.now,'delivery_deadline_expired')
    kind,manifest,refs=await _package(app,tx,order)
    return _insert_delivery(tx,order,ctx,request,kind,manifest,refs)


async def accept_order(app,tx,order,ctx,request,delivery_digest,*,automatic=False):
    delivery=_delivery(tx,order['id'])
    await _verified_delivery(app,tx,order,delivery)
    require(delivery['delivery_digest']==delivery_digest,'delivery_mismatch')
    require(order['state']=='delivered' and order['delivered_at'] is not None and
            delivery['state']=='prepared','delivery_not_acceptable')
    require(not automatic or order['escrow_policy']=='managed-instant-v1','automatic_settlement_forbidden')
    require(order['delivery_target']=={'subject_id':order['buyer'],'channel':'site'},'delivery_recipient_mismatch')
    require(_balance(tx,order['escrow_subject'])==order['total_price_minor'],'escrow_balance_mismatch')
    # An automatic transition is policy consent in the signed quote, NOT a
    # fabricated buyer signature asserting that the goods have been inspected.
    engine.transition(tx,order,'accepted',ctx,request,'policy_delivery' if automatic else 'buyer_accepted')
    receipt=_post_transfer(tx,sender=order['escrow_subject'],recipient=order['seller'],
        amount=order['total_price_minor'],actor=order['buyer'],request_id=request.request_id,
        now=ctx.now,receipt_signer=app.receipt_signer,reference='order_release:'+order['id'],entry_key='order_release')
    # Policy settlement says the deposit is available, not that a client got it.
    changed=tx.execute("UPDATE store_deliveries SET state=?,claimed_at=?,receipt=? WHERE order_id=? AND state='prepared'",
        ('prepared' if automatic else 'claimed',None if automatic else wire(ctx.now),
         canonical(receipt).decode(),order['id']),write=True)
    require(changed.rowcount==1,'delivery_not_acceptable')
    order['receipt_refs']=[*order['receipt_refs'],receipt['body']['transaction_id']]
    tx.execute('UPDATE store_orders SET settled_at=?,receipt_refs=? WHERE id=?',
               (wire(ctx.now),canonical(order['receipt_refs']).decode(),order['id']),write=True)
    engine.transition(tx,order,'settled',ctx,request,'escrow_released')
    return {'order_id':order['id'],'state':'settled','delivery_id':delivery['id'],'receipt':receipt}


async def objective_fault(app,tx,order,now):
    """Only observable missing/corrupt delivery or an expired undelivered order.

    No caller supplies a reason, amount or recipient. Temporary infrastructure
    failures and a subjective bad-service/decryption complaint are not evidence.
    """
    delivery=_delivery(tx,order['id'])
    if order['state']=='funded' and order['delivered_at'] is None and engine.deadline(order)<=now:
        return 'delivery_deadline_expired'
    if order['delivered_at'] is not None and delivery is None:
        return 'missing_delivery'
    try:
        if order['delivery_mode']=='managed_instant':
            await _package(app,tx,order)
        if delivery is not None:
            await _verified_delivery(app,tx,order,delivery)
    except FileNotFoundError:
        return 'missing_package_content'
    except Failure as exc:
        if exc.code in {'delivery_package_mismatch','delivery_mismatch','content_not_found',
                        'blob_not_found','content_digest_mismatch','content_missing',
                        'content_size_mismatch','content_truncated','invalid_json'}:
            return 'delivery_integrity_failure'
        raise
    return None


def install(app):
    op,finish=registration(app,'delivery',('orders',))

    @op('delivery.prepare',obj({'order_id':IDENTIFIER},('order_id',)),signature=True, requirements=account_requirements)
    async def prepare(ctx,request,tx):
        order=_buyer_order(tx,request.arguments['order_id'],_subject(ctx))
        return HandlerOutput(data={'delivery':await prepare_order(app,tx,order,ctx,request)})

    @op('delivery.get',obj({'order_id':IDENTIFIER},('order_id',)),effect='read', requirements=account_requirements)
    async def get(ctx,request,tx):
        buyer=ctx.principal.subject
        require(buyer is not None and ctx.principal.actor==buyer,'order_not_found')
        order=_buyer_order(tx,request.arguments['order_id'],buyer)
        require(order['state'] not in {'cancelled','refunded'},'delivery_not_found')
        delivery=_delivery(tx,order['id'])
        require(delivery is not None,'delivery_not_found')
        await _verified_delivery(app,tx,order,delivery)
        return HandlerOutput(data={'delivery':{
            'delivery_id':delivery['id'],'order_id':order['id'],'recipient_subject':buyer,
            'kind':delivery['kind'],'manifest':delivery['manifest'],'package_digest':delivery['package_digest'],
            'delivery_digest':delivery['delivery_digest'],'channel':'site','state':delivery['state'],
            'prepared_at':delivery['prepared_at'],'claimed_at':delivery['claimed_at'],
            'payloads':await _payload(app,delivery)}})

    @op('delivery.accept',obj({'order_id':IDENTIFIER,
        'delivery_digest':{'type':'string','pattern':'^sha256:[a-f0-9]{64}$'}},
        ('order_id','delivery_digest')),signature=True, requirements=account_requirements)
    async def accept(ctx,request,tx):
        order=_buyer_order(tx,request.arguments['order_id'],_subject(ctx))
        return HandlerOutput(data=await accept_order(app,tx,order,ctx,request,request.arguments['delivery_digest']))

    @op('delivery.claim',obj({'order_id':IDENTIFIER,
        'delivery_digest':{'type':'string','pattern':'^sha256:[a-f0-9]{64}$'}},
        ('order_id','delivery_digest')),signature=True, requirements=account_requirements)
    async def claim(ctx,request,tx):
        order=_buyer_order(tx,request.arguments['order_id'],_subject(ctx))
        require(order['state']=='settled' and order['escrow_policy']=='managed-instant-v1',
                'delivery_not_claimable')
        delivery=_delivery(tx,order['id'])
        await _verified_delivery(app,tx,order,delivery)
        require(delivery['delivery_digest']==request.arguments['delivery_digest'],'delivery_mismatch')
        require(delivery['state']=='prepared','delivery_already_claimed')
        tx.execute("UPDATE store_deliveries SET state='claimed',claimed_at=? WHERE id=?",
                   (wire(ctx.now),delivery['id']),write=True)
        return HandlerOutput(data={'order_id':order['id'],'delivery_id':delivery['id'],
                                  'state':'claimed','claimed_at':wire(ctx.now)})

    @op('delivery.submit',obj({'order_id':IDENTIFIER,'payload_ref':REF,'recipient_key_id':IDENTIFIER,
        'completion_statement':{'type':'string','minLength':1,'maxLength':16384}},('order_id',)),signature=True, requirements=account_requirements)
    async def submit(ctx,request,tx):
        seller=_subject(ctx)
        args=request.arguments
        order=order_row(tx,args['order_id'],seller)
        require(order['seller']==seller,'order_not_found')
        require(order['state']=='funded' and order['delivered_at'] is None,'order_not_deliverable')
        require(engine.deadline(order)>ctx.now,'delivery_deadline_expired')
        if order['delivery_mode']=='sealed_manual':
            require(set(args)=={'order_id','payload_ref','recipient_key_id'},'invalid_manual_delivery')
            key=tx.one('''SELECT key_id,recipient FROM encryption_subkeys WHERE subject=?
                AND is_primary=1 AND retired_at IS NULL''',(order['buyer'],))
            require(key is not None and key[0]==args['recipient_key_id'],'recipient_key_changed')
            ref=decode(ResourceRef,args['payload_ref'])
            require(ref.revision is not None,'payload_revision_required')
            source=await tx.resource(await resolve(tx,ref.id))
            require(source.owner==seller and source.type in {'file','attachment'} and source.state=='active','payload_not_owned')
            from msg.plugins.communication import direct_ancestor
            require(await direct_ancestor(tx,source.id) is None,'private_payload_forbidden')
            await check_access(app,ctx,request,tx,source.id,'read')
            revision=await tx.revision(ref)
            data=await app.contents.read_bytes(revision.content,limit=MAX_INLINE_BYTES)
            require(data.startswith((b'age-encryption.org/v1\n',b'-----BEGIN AGE ENCRYPTED FILE-----')),
                    'invalid_ciphertext_envelope')
            require(digest(data)==revision.content.digest,'delivery_package_mismatch')
            refs=[{'id':source.id,'revision':revision.id,'blob':wire(revision.content)}]
            manifest={'recipient_key_id':key[0],'recipient':key[1],'verification':'buyer_decrypts'}
            kind='sealed'
            await app.contents.pin(revision.content,'order:'+order['id'])
        else:
            require(order['delivery_mode']=='service' and set(args)=={'order_id','completion_statement'},'invalid_manual_delivery')
            kind,refs='service',[]
            manifest={'completion_statement':args['completion_statement'],'verification':'buyer_accepts_service'}
        delivered=_insert_delivery(tx,order,ctx,request,kind,manifest,refs)
        return HandlerOutput(data={'delivery':delivered})
    finish()
