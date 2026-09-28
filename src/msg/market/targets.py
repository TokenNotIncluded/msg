"""Buyer-owned endpoint binding shared by checkout, delivery and SMTP worker."""
from __future__ import annotations

import re
from dataclasses import replace

from msg.core.codec import canonical, decode, digest, loads
from msg.core.errors import Failure, require
from msg.core.models import EffectJob, EmailSettings
from msg.market.policy import contract
from msg.plugins.common import new_id


def mail_enabled(app):
    config = app.settings.server.mail
    return config is not None and config.enabled


def _email(tx, subject):
    row = tx.one('SELECT generation,body FROM emails WHERE subject=?', (subject,))
    if not row:
        return None, None
    settings = decode(EmailSettings, loads(row[1]))
    endpoint = 'em_' + digest((subject, settings.address, settings.verified_at, row[0]))[7:39]
    return settings, endpoint


async def target_for(app, tx, buyer, email_address=None):
    handle = (await tx.resource(buyer)).name
    target = {'subject_id': buyer, 'channel': 'site', 'handle_snapshot': handle,
              'email': {'state': 'disabled'}}
    if email_address is None:
        return target
    require(isinstance(email_address, str) and len(email_address) <= 254 and
            re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email_address) is not None and
            '\r' not in email_address and '\n' not in email_address, 'invalid_email')
    email, endpoint = _email(tx, buyer)
    verified = (email is not None and email.subject_id == buyer and
                email.address == email_address and email.verified_at is not None)
    target['email'] = {'state': 'pending' if mail_enabled(app) else 'disabled',
        'subject_id': buyer, 'address_snapshot': email_address,
        'endpoint_id': endpoint if verified else None}
    # Verification is a separate, order-information-free external effect.
    return target


def save_target(tx, order, target):
    tx.execute('UPDATE store_orders SET delivery_target=? WHERE id=?',
               (canonical(target).decode(), order['id']), write=True)
    order['delivery_target'] = target


def validate_target(tx, order, *, email=False, encryption=False):
    target = order['delivery_target']
    require(target.get('subject_id') == order['buyer'] and target.get('channel') == 'site',
            'delivery_recipient_mismatch')
    if encryption:
        locked = contract(tx, order['id']).get('recipient_key')
        row = tx.one('SELECT subject,recipient,retired_at FROM encryption_subkeys WHERE key_id=?',
                     (locked['key_id'],)) if locked else None
        require(row is not None and row[0] == order['buyer'] and row[1] == locked['recipient']
                and row[2] is None, 'delivery_recipient_mismatch')
    if email:
        chosen = target.get('email', {})
        require(not chosen.get('notification_disabled'), 'delivery_recipient_mismatch')
        endpoint_id = chosen.get('endpoint_id') or ''
        if endpoint_id.startswith('dep_'):
            row = tx.one('SELECT subject,address,active FROM delivery_email_endpoints WHERE id=?',
                         (endpoint_id,))
            require(row is not None and row[2] and
                    row[0] == chosen.get('subject_id') == order['buyer'] and
                    row[1] == chosen.get('address_snapshot'), 'delivery_recipient_mismatch')
            return row[1]
        current, endpoint = _email(tx, order['buyer'])
        require(chosen.get('subject_id') == order['buyer'] and current is not None and
                current.subject_id == order['buyer'] and current.verified_at is not None and
                chosen.get('endpoint_id') == endpoint and
                chosen.get('address_snapshot') == current.address,
                'delivery_recipient_mismatch')
        return current.address
    return None


async def enqueue_notification(app, tx, ctx, request, order):
    chosen = order['delivery_target'].get('email', {})
    if (not mail_enabled(app) or not chosen.get('endpoint_id') or
            chosen.get('notification_disabled')):
        return
    try:
        validate_target(tx, order, email=True)
    except Failure as exc:
        if exc.code != 'delivery_recipient_mismatch':
            raise
        save_target(tx, order, {**order['delivery_target'], 'email': {
            **chosen, 'state': 'pending', 'error': exc.code}})
        return
    # Only the buyer can bind/queue the auxiliary channel, even when the seller
    # uploads ciphertext later. Checkout's signed principal is persisted privately.
    locked = contract(tx, order['id'])
    from msg.core.models import Principal
    principal = decode(Principal, locked['buyer_principal'])
    dedupe = 'market-mail:'+order['id']+':'+chosen['endpoint_id']
    if tx.one('SELECT 1 FROM jobs WHERE dedupe=?', (dedupe,)):
        return
    job = EffectJob(id=new_id('job'), event_id='delivery:'+order['id'], kind='market_mail',
        dedupe_key=dedupe, principal=principal,
        operation='orders.buy', arguments={'order_id': order['id'], 'endpoint_id': chosen['endpoint_id']},
        state='pending', attempts=0, next_attempt_at=ctx.now, lease_until=None)
    await tx.enqueue(job)
    target = {**order['delivery_target'], 'email': {**chosen, 'state': 'queued'}}
    save_target(tx, order, target)


async def render_notification(app, tx, job):
    from msg.plugins.delivery import _delivery
    from msg.plugins.orders import _row
    order = _row(tx, job.arguments['order_id'], job.principal.subject)
    require(order['buyer'] == job.principal.subject and job.principal.actor == order['buyer'],
            'delivery_recipient_mismatch')
    delivery = _delivery(tx, order['id'])
    require(order['state'] in {'delivered','accepted','settled'} and delivery is not None and
            delivery['recipient_subject'] == order['buyer'], 'delivery_not_available')
    require(order['delivery_target'].get('email', {}).get('endpoint_id') == job.arguments['endpoint_id'],
            'delivery_recipient_mismatch')
    locked = contract(tx, order['id'])
    address = validate_target(tx, order, email=True,
        encryption=locked['listing']['delivery_mode'] == 'sealed_manual')
    # A stable site URL is not a capability. GET always requires fresh authentication.
    link = app.settings.service_url.rstrip('/') + '/_orders/' + order['id'] + '/_delivery'
    body = {'order_id': order['id'], 'handle': locked['handle_snapshot'], 'pickup': link}
    return replace(job, kind='mail', arguments={'recipient': address,
        'recipient_subject': order['buyer'], 'subject': 'msg order notification',
        'text': canonical(body).decode()})


def notification_status(tx, job, state, code):
    """Update only the endpoint this attempt was authorised to notify."""
    if job.kind != 'market_mail':
        return
    from msg.plugins.orders import _row
    order = _row(tx, job.arguments['order_id'], job.principal.subject)
    chosen = order['delivery_target'].get('email', {})
    if (chosen.get('endpoint_id') != job.arguments['endpoint_id'] or
            chosen.get('notification_disabled')):
        return
    status = ('smtp_accepted' if state == 'done' and code == 'sent' else
              'delivered_unknown' if state == 'uncertain' else
              'disabled' if code == 'mail_disabled' else 'pending')
    save_target(tx, order, {**order['delivery_target'], 'email': {
        **chosen, 'state': status, 'last_result': code}})
