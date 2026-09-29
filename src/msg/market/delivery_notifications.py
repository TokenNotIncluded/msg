"""Optional, buyer-bound email hints; never delivery payloads or payment work.

Facts live in the transactional metadata settings store, not a cache or user
resource. A signed checkout email is per-order opt-in. An unmatched address stays
pending until the buyer explicitly verifies it through identity.email_* and
calls delivery.notify. Checkout never overwrites an existing mailbox.
"""

from __future__ import annotations

from dataclasses import replace

from msg.core.errors import require
from msg.core.models import EffectJob
from msg.market.delivery_targets import (
    email_binding,
    mail_enabled,
    pickup_url,
    validate_email_binding,
    validate_target,
)
from msg.plugins.common import new_id
from msg.plugins.communication import event_id


def _key(order_id):
    return 'delivery_notification:' + order_id


def _fact(tx, order):
    fact = tx.setting(_key(order['id']))
    require(
        fact is not None
        and fact.get('version') == 1
        and fact.get('order_id') == order['id']
        and fact.get('buyer') == order['buyer'],
        'delivery_notification_unavailable',
    )
    validate_target(order)
    return dict(fact)


async def notification_status(app, tx, order_id):
    fact = tx.setting(_key(order_id))
    if not fact:
        return {'state': 'disabled'}
    if fact['job_id']:
        job = await tx.job(fact['job_id'])
        state = {
            'pending': 'queued',
            'running': 'queued',
            'done': 'smtp_accepted',
            'uncertain': 'delivered_unknown',
            'failed': 'failed',
        }[job.state]
        if job.state in {'pending', 'running'} and (not fact['enabled'] or not mail_enabled(app)):
            state = 'disabled'
        return {'state': state}
    return {'state': 'pending' if fact['enabled'] and mail_enabled(app) else 'disabled'}


async def initialize_notification(app, ctx, request, tx, order, address):
    if address is None:
        return {'state': 'disabled'}
    validate_target(order)
    handle = (await tx.resource(order['buyer'])).name
    fact = {
        'version': 1,
        'order_id': order['id'],
        'buyer': order['buyer'],
        'buyer_handle': handle,
        'address': address,
        'enabled': True,
        'endpoint': email_binding(tx, order['buyer'], address),
        'job_id': None,
    }
    tx.set_setting(_key(order['id']), fact)
    return await queue_notification(app, ctx, request, tx, order, enabled=True)


async def queue_notification(app, ctx, request, tx, order, *, enabled):
    fact = _fact(tx, order)
    require(ctx.principal.actor == ctx.principal.subject == order['buyer'], 'order_not_found')
    require(order['state'] in {'delivered', 'settled'}, 'order_not_deliverable')
    fact['enabled'] = enabled
    tx.set_setting(_key(order['id']), fact)
    if not enabled or not mail_enabled(app) or fact['job_id'] is not None:
        # A failed/uncertain external attempt is never revived by another request.
        return await notification_status(app, tx, order['id'])
    if fact['endpoint'] is None:
        fact['endpoint'] = email_binding(tx, order['buyer'], fact['address'])
    if fact['endpoint'] is None:
        return {'state': 'pending'}
    validate_email_binding(tx, order, fact['endpoint'])
    job_id = new_id('job')
    fact['job_id'] = job_id
    tx.set_setting(_key(order['id']), fact)
    await tx.enqueue(
        EffectJob(
            id=job_id,
            event_id=event_id(request, order['buyer']),
            kind='mail',
            dedupe_key='order:' + order['id'] + ':' + order['buyer'] + ':mail',
            principal=ctx.principal,
            operation=request.operation,
            arguments={
                'order_id': order['id'],
                'recipient_subject': order['buyer'],
                'contract_version': request.contract_version,
                'order_notification': True,
            },
            state='pending',
            attempts=0,
            next_attempt_at=ctx.now,
            lease_until=None,
        )
    )
    return {'state': 'queued'}


async def project_notification(app, tx, job, principal):
    """Reconstruct the allowlisted message only after every live owner check."""
    from msg.market.managed_delivery import read_delivery as _delivery
    from msg.market.managed_delivery import verify_managed_delivery as _verified_delivery
    from msg.market.order_records import read_order as _row

    require(mail_enabled(app), 'mail_disabled')
    require(
        (job.operation, job.arguments.get('contract_version'))
        in {('orders.buy', 2), ('delivery.notify', 1)},
        'invalid_delivery_notification',
    )
    buyer = job.arguments['recipient_subject']
    require(principal.actor == principal.subject == buyer, 'delivery_recipient_mismatch')
    await app.authorizer.require_base(
        principal, f'{job.operation}@{job.arguments["contract_version"]}', buyer, tx
    )
    order = _row(tx, job.arguments['order_id'], buyer)
    require(
        order['buyer'] == buyer and order['state'] in {'delivered', 'settled'},
        'delivery_recipient_mismatch',
    )
    fact = _fact(tx, order)
    require(fact['job_id'] == job.id and fact['enabled'], 'notification_disabled')
    delivery = _delivery(tx, order['id'])
    await _verified_delivery(app, tx, order, delivery)
    recipient = validate_email_binding(tx, order, fact['endpoint'])
    require(fact['address'] == recipient, 'delivery_recipient_mismatch')
    link = pickup_url(app, order['id'])
    text = order['id'] + '\n' + fact['buyer_handle'] + '\n' + link + '\n'
    # Never forward a caller/job-selected subject, text, attachment or token.
    return replace(
        job,
        arguments={
            'recipient': recipient,
            'recipient_subject': buyer,
            'subject': 'msg order ready',
            'text': text,
        },
    )
