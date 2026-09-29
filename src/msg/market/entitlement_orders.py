"""New explicit synchronous redemption contract using the existing Order writer."""
from msg.constants import ROOT_SUBJECT
from msg.core.codec import b64, canonical, digest, wire
from msg.core.errors import require
from msg.market.escrow import settle
from msg.market.order_records import new_order_id, read_order
from msg.market.orders import fund, view
from msg.market.policy import ENTITLEMENT_POLICY as POLICY
from msg.market.targets import target_for



async def redeem(app, tx, ctx, request, offer, fulfill):
    """No Purchase row or Delivery; grant and normal Order settlement commit together."""
    buyer, args = ctx.principal.subject, request.arguments
    resource = await tx.resource(offer['offer_id'])
    require(resource.owner == ROOT_SUBJECT and resource.revision == args['listing_revision'],
            'offer_listing_changed')
    require(args['offer_snapshot_digest'] == digest(offer), 'offer_snapshot_changed')
    total = offer['price_minor'] * args['quantity']
    require(total == args['total_price_minor'], 'price_changed')
    from msg.market.offer_resources import authoritative_offer, listing_body
    source = await authoritative_offer(app, tx, offer['offer_id'])
    require(source is not None, 'offer_requires_import')
    listing = listing_body(source)
    target = await target_for(app, tx, buyer)
    order_id, now = new_order_id(), wire(ctx.now)
    from msg.market.order_resources import begin_new
    begin_new(tx, order_id)
    escrow = 'esc_' + order_id[4:]
    tx.execute("INSERT INTO ledger_accounts(id,kind,subject_id,source_id) VALUES (?,'order_escrow',NULL,?)",
               (escrow, order_id), write=True)
    tx.execute('''INSERT INTO store_orders
        (id,buyer,seller,listing_id,listing_revision,package_id,package_revision,package_digest,
         quantity,unit_price_minor,total_price_minor,currency_id,escrow_subject,escrow_policy,
         dispute_policy,terms_digest,delivery_target,payment_intent_digest,payment_transaction_id,
         state,created_at,funded_at,receipt_refs)
         VALUES (?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,?,NULL,'created',?,NULL,'[]')''',
        (order_id, buyer, ROOT_SUBJECT, resource.id, resource.revision, args['quantity'],
         offer['price_minor'], total, 'primary', escrow, listing['escrow_policy'],
         listing['dispute_policy'], digest(listing['terms']), canonical(target).decode(),
         request.payload_digest, now), write=True)
    credential = await tx.credential(ctx.principal.credential_id)
    locked = {'order_id': order_id, 'version': 5, 'resource_model': 1, 'buyer': buyer, 'seller': ROOT_SUBJECT,
        'listing': listing, 'listing_id': resource.id, 'listing_revision': resource.revision,
        'escrow_subject': escrow, 'package_id': None, 'package_revision': None,
        'package_digest': None, 'quantity': args['quantity'], 'total_price_minor': total,
        'terms_digest': digest(listing['terms']),
        'policy': {'policy': POLICY, 'policy_digest': digest(POLICY)},
        'settlement_policy': {'id': 'deterministic-entitlement-v1', 'version': 1},
        'offer_snapshot': offer, 'redemption_request_id': request.request_id,
        'redemption_request': wire(request), 'redemption_signer': b64(credential.verifier),
        'recipient_key': None, 'created_at': now}
    tx.execute('INSERT INTO order_contracts(order_id,body,digest) VALUES (?,?,?)',
               (order_id, canonical(locked).decode(), digest(locked)), write=True)
    order = read_order(tx, order_id, buyer)
    funding = await fund(app, tx, ctx, request, order)
    purchase_input = {'subject_id': buyer, 'request_id': request.request_id,
                      'quantity': args['quantity'], 'offer_snapshot': offer}
    receipts = await settle(app, tx, order, now=ctx.now, actor=buyer,
        request_id=request.request_id, reason='deterministic_entitlement',
        fulfill=lambda tid: fulfill(app, tx, purchase_input, ctx.now, tid))
    return {'order': view(tx, read_order(tx, order_id, buyer), buyer),
            'funding': funding, 'settlement': receipts[0]}
