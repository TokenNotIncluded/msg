"""Optional checkout-address verification, separate from account email settings.

The emailed proof cannot authenticate or identify an order. Verification also
requires the original buyer's current signed request. An endpoint never changes
owner; a disabled endpoint is not reactivated by retries or a restored job.
"""
from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import timedelta

from msg.core.codec import b64, canonical, digest, parse_time, wire
from msg.core.errors import require
from msg.core.models import EffectJob, HandlerOutput
from msg.market.targets import enqueue_notification, mail_enabled, save_target, validate_target
from msg.plugins.common import new_id
from msg.market.order_records import read_order as _row, require_signed_subject as _subject
from msg.plugins.schemas import IDENTIFIER, obj


async def verification_message(app, tx, ctx, request, order):
    chosen = order['delivery_target'].get('email', {})
    if (not mail_enabled(app) or chosen.get('endpoint_id') or
            not chosen.get('address_snapshot')):
        return
    token, challenge_id = b64(secrets.token_bytes(32)), new_id('evc')
    tx.execute('''INSERT INTO delivery_email_challenges
        (id,order_id,subject,address,verifier,expires_at) VALUES (?,?,?,?,?,?)''',
        (challenge_id,order['id'],order['buyer'],chosen['address_snapshot'],digest(token),
         wire(ctx.now+timedelta(minutes=15))), write=True)
    save_target(tx, order, {**order['delivery_target'], 'email': {
        **chosen, 'challenge_id':challenge_id, 'state':'pending'}})
    await tx.enqueue(EffectJob(id=new_id('job'),event_id=challenge_id,kind='market_email_verify',
        dedupe_key='market-verify:'+challenge_id,principal=ctx.principal,
        operation=request.operation,arguments={'contract_version':request.contract_version,'challenge_id':challenge_id,'token':token},
        state='pending',attempts=0,next_attempt_at=ctx.now,lease_until=None))


async def render_verification(app, tx, job):
    row = tx.one('''SELECT order_id,subject,address,verifier,expires_at,consumed
        FROM delivery_email_challenges WHERE id=?''', (job.arguments['challenge_id'],))
    require(row is not None and not row[5] and parse_time(row[4])>app.clock() and
            row[1] == job.principal.subject == job.principal.actor and
            digest(job.arguments['token']) == row[3], 'email_challenge_expired')
    order = _row(tx,row[0],row[1])
    chosen = order['delivery_target'].get('email',{})
    require(order['buyer']==row[1] and chosen.get('challenge_id')==job.arguments['challenge_id'] and
            chosen.get('address_snapshot')==row[2] and chosen.get('endpoint_id') is None and
            order['state'] in {'created','funded','delivered','settled'}, 'email_challenge_expired')
    # No order ID, buyer handle, goods, pickup link or authority in this text.
    return replace(job,kind='mail',arguments={'recipient':row[2],
        'recipient_subject':row[1], 'subject':'msg address verification',
        'text':canonical({'challenge_id':job.arguments['challenge_id'],
            'token':job.arguments['token'], 'purpose':'verify this address only'}).decode()})


def install(app, op):
    from msg.market.orders import ORDER, view

    @op('orders.email_verify',obj({'order_id':IDENTIFIER,'challenge_id':IDENTIFIER,
        'token':{'type':'string','minLength':43,'maxLength':43}},
        ('order_id','challenge_id','token')),signature=True)
    async def verify(ctx, request, tx):
        a, buyer = request.arguments, _subject(ctx)
        order = _row(tx,a['order_id'],buyer)
        require(order['buyer']==buyer,'order_not_found')
        row = tx.one('''SELECT order_id,subject,address,verifier,expires_at,consumed
            FROM delivery_email_challenges WHERE id=?''',(a['challenge_id'],))
        chosen = order['delivery_target'].get('email',{})
        require(row is not None and row[0]==order['id'] and row[1]==buyer and not row[5] and
                parse_time(row[4])>ctx.now and secrets.compare_digest(row[3],digest(a['token'])) and
                chosen.get('challenge_id')==a['challenge_id'] and
                chosen.get('address_snapshot')==row[2] and chosen.get('endpoint_id') is None and
                order['state'] in {'created','funded','delivered','settled'},'email_challenge_invalid')
        endpoint = new_id('dep')
        tx.execute('INSERT INTO delivery_email_endpoints(id,subject,address,verified_at) VALUES (?,?,?,?)',
            (endpoint,buyer,row[2],wire(ctx.now)),write=True)
        tx.execute('UPDATE delivery_email_challenges SET consumed=TRUE WHERE id=?',
            (a['challenge_id'],),write=True)
        chosen = {k:v for k,v in chosen.items() if k!='challenge_id'}
        save_target(tx,order,{**order['delivery_target'],'email':{
            **chosen,'endpoint_id':endpoint,'state':'pending'}})
        validate_target(tx,order,email=True)
        if order['delivered_at']:
            await enqueue_notification(app,tx,ctx,request,order)
        return HandlerOutput(data={'order':view(tx,order,buyer)})

    @op('orders.email_disable',ORDER,signature=True)
    async def disable(ctx, request, tx):
        buyer = _subject(ctx)
        order = _row(tx,request.arguments['order_id'],buyer)
        require(order['buyer']==buyer,'order_not_found')
        chosen = order['delivery_target'].get('email',{})
        if (chosen.get('endpoint_id') or '').startswith('dep_'):
            tx.execute('UPDATE delivery_email_endpoints SET active=FALSE WHERE id=? AND subject=?',
                (chosen['endpoint_id'],buyer),write=True)
        save_target(tx,order,{**order['delivery_target'],'email':{
            **chosen,'state':'disabled','notification_disabled':True}})
        return HandlerOutput(data={'order':view(tx,order,buyer)})
