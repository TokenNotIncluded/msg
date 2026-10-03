"""Signed, self-owned machine admission and opt-in finite game notifications."""

from msg.core.codec import wire
from msg.core.errors import require
from msg.core.models import HandlerOutput
from msg.plugins.common import operation_id, registration
from msg.plugins.communication import _signed_subject
from msg.plugins.schemas import obj
from msg.transports.game_api import GAME_EVENTS, game_runtime, subscription_key


def install(app):
    op, finish = registration(app, 'game', ('communication',))

    @op(
        'communication.game_join_ticket',
        obj(
            {
                'nonce': {
                    'type': 'string',
                    'minLength': 43,
                    'maxLength': 43,
                    'pattern': '^[A-Za-z0-9_-]+$',
                }
            },
            ('nonce',),
        ),
        signature=True,
        requires_rules=('msg.auth', 'msg.protocol'),
    )
    async def join_ticket(ctx, request, tx):
        owner = _signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal, operation_id(request), owner, tx)
        resource = await tx.resource(owner)
        subject = await tx.subject(owner)
        require(
            resource.type == 'user' and resource.state == 'active' and not subject.local_only,
            'invalid_grant',
        )
        runtime = game_runtime(app)
        runtime.configure(owner, tx.setting(subscription_key(owner)))
        return HandlerOutput(data=runtime.issue(ctx.principal, request.arguments['nonce']))

    @op(
        'communication.game_webhook_subscribe',
        obj(
            {
                'events': {
                    'type': 'array',
                    'items': {'enum': sorted(GAME_EVENTS)},
                    'uniqueItems': True,
                    'minItems': 1,
                    'maxItems': len(GAME_EVENTS),
                }
            },
            ('events',),
        ),
        signature=True,
        requires_rules=('msg.auth', 'msg.protocol'),
    )
    async def subscribe(ctx, request, tx):
        owner = _signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal, operation_id(request), owner, tx)
        resource = await tx.resource(owner)
        subject = await tx.subject(owner)
        require(
            resource.type == 'user' and resource.state == 'active' and not subject.local_only,
            'invalid_grant',
        )
        row = tx.one('SELECT enabled,generation FROM webhook_endpoints WHERE subject=?', (owner,))
        require(row is not None and row[0] == 1, 'webhook_disabled')
        old = tx.setting(subscription_key(owner), {})
        record = {
            'enabled': True,
            'events': sorted(request.arguments['events']),
            'generation': old.get('generation', 0) + 1,
            'endpoint_generation': row[1],
            'principal': wire(ctx.principal),
        }
        tx.set_setting(subscription_key(owner), record)
        game_runtime(app).configure(owner, record)
        return HandlerOutput(
            data={key: value for key, value in record.items() if key != 'principal'}
        )

    @op(
        'communication.game_webhook_unsubscribe',
        obj(),
        signature=True,
        requires_rules=('msg.auth', 'msg.protocol'),
    )
    async def unsubscribe(ctx, request, tx):
        owner = _signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal, operation_id(request), owner, tx)
        old = tx.setting(subscription_key(owner), {})
        record = {'enabled': False, 'events': [], 'generation': old.get('generation', 0) + 1}
        tx.set_setting(subscription_key(owner), record)
        game_runtime(app).configure(owner, record)
        return HandlerOutput(data=record)

    @op(
        'communication.game_webhook_status',
        obj(),
        effect='read',
        signature=True,
        requires_rules=('msg.auth', 'msg.protocol'),
    )
    async def status(ctx, request, tx):
        owner = _signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal, operation_id(request), owner, tx)
        record = tx.setting(subscription_key(owner), {})
        return HandlerOutput(
            data={
                'enabled': bool(record.get('enabled')),
                'events': record.get('events', []),
                'generation': record.get('generation', 0),
                'delivery': 'best_effort_before_durable_enqueue',
            }
        )

    finish()
