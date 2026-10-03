"""Owner-only discrete game effects; no world snapshots or tick-time I/O.

Only the trusted game producer calls ``enqueue_game_event``. It must resolve the
machine's owner before freezing the event and both subscription generations.
Jobs contain bounded facts, not public MSG resources or authority to read peers.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from msg.core.codec import canonical, decode, loads
from msg.core.errors import require
from msg.core.models import EffectJob, Principal
from msg.plugins.common import new_id
from msg.storage.capacity import require_webhook_capacity
from msg.workers.effects import current_principal
from msg.workers.webhook import open_secret, validate_endpoint

OPERATION = 'communication.game_webhook_subscribe'
EVENT_TYPES = frozenset({'game.joined', 'game.left', 'game.region', 'game.collect', 'game.hit'})
MAX_EVENT_BYTES = 2048
_REQUIRED = frozenset({'id', 'type', 'ship_id', 'time_ms'})
_OPTIONAL = frozenset({'region', 'score', 'collected', 'hp'})
_MAX_INTEGER = 2**53 - 1


def bounded_game_event(event):
    """Copy a closed, owner-only vocabulary before crossing an async boundary."""
    require(
        isinstance(event, Mapping) and _REQUIRED <= event.keys() <= _REQUIRED | _OPTIONAL,
        'invalid_game_event',
    )
    require(
        type(event['id']) is str
        and re.fullmatch(r'ge_[A-Za-z0-9_-]{1,156}', event['id']) is not None
        and type(event['ship_id']) is str
        and re.fullmatch(r'ship_[a-f0-9]{24}', event['ship_id']) is not None
        and type(event['type']) is str
        and event['type'] in EVENT_TYPES,
        'invalid_game_event',
    )
    require(
        type(event['time_ms']) is int and 0 <= event['time_ms'] <= _MAX_INTEGER,
        'invalid_game_event',
    )
    if 'region' in event:
        require(type(event['region']) is int and 0 <= event['region'] < 19, 'invalid_game_event')
    for name in ('score', 'collected'):
        if name in event:
            require(
                type(event[name]) is int and 0 <= event[name] <= _MAX_INTEGER,
                'invalid_game_event',
            )
    if 'hp' in event:
        require(
            type(event['hp']) in {int, float}
            and math.isfinite(event['hp'])
            and 0 <= event['hp'] <= 100,
            'invalid_game_event',
        )
    value = dict(event)
    require(len(canonical(value)) <= MAX_EVENT_BYTES, 'game_event_too_large')
    return value


async def _authority(
    app, tx, subject, subscription_generation, endpoint_generation, *, captured=None
):
    app.runtime_generation.require_current(tx)
    require(not app.executor.recovery_drill_active(), 'recovery_quarantined')
    require(
        type(subscription_generation) is int
        and 1 <= subscription_generation <= _MAX_INTEGER
        and type(endpoint_generation) is int
        and 1 <= endpoint_generation <= _MAX_INTEGER,
        'invalid_game_subscription',
    )
    owner = await tx.subject(subject)
    resource = await tx.resource(subject)
    require(
        not owner.local_only
        and resource.type == 'user'
        and resource.owner == subject
        and resource.state == 'active',
        'game_webhook_owner',
    )
    subscription = tx.setting('game_webhook:' + subject)
    require(
        type(subscription) is dict
        and subscription.get('enabled') is True
        and subscription.get('generation') == subscription_generation
        and subscription.get('endpoint_generation') == endpoint_generation,
        'game_webhook_subscription_disabled',
    )
    require(type(subscription.get('principal')) is dict, 'game_webhook_owner')
    original = decode(Principal, subscription['principal'])
    require(
        original.subject == subject
        and original.actor == subject
        and original.method == 'signature'
        and (captured is None or original == captured),
        'game_webhook_owner',
    )
    principal = await current_principal(app, original, tx)
    await app.authorizer.require_base(principal, OPERATION + '@1', subject, tx)
    endpoint = tx.one(
        'SELECT url,nonce,ciphertext,enabled,generation FROM webhook_endpoints WHERE subject=?',
        (subject,),
    )
    require(
        endpoint is not None and endpoint[3] == 1 and endpoint[4] == endpoint_generation,
        'webhook_disabled',
    )
    validate_endpoint(endpoint[0])
    return original, subscription, endpoint


async def enqueue_game_event(app, subject, event, subscription_generation, endpoint_generation):
    """Called by the dedicated game consumer, never by a physics tick."""
    event = bounded_game_event(event)
    async with app.metadata.transaction(write=True) as tx:
        principal, subscription, _ = await _authority(
            app, tx, subject, subscription_generation, endpoint_generation
        )
        require(event['type'] in subscription.get('events', ()), 'game_event_not_subscribed')
        dedupe = f'{event["id"]}:{subject}:game:webhook'
        existing = tx.one("SELECT body FROM jobs WHERE kind='webhook' AND dedupe=?", (dedupe,))
        if existing is not None:
            previous = decode(EffectJob, loads(existing[0]))
            require(
                previous.operation == OPERATION
                and previous.principal == principal
                and previous.arguments
                == {
                    'contract_version': 1,
                    'recipient_subject': subject,
                    'subscription_generation': subscription_generation,
                    'endpoint_generation': endpoint_generation,
                    'event': event,
                },
                'game_event_conflict',
            )
            return previous.id
        require_webhook_capacity(tx)
        job = EffectJob(
            id=new_id('job'),
            event_id=event['id'],
            kind='webhook',
            dedupe_key=dedupe,
            principal=principal,
            operation=OPERATION,
            arguments={
                'contract_version': 1,
                'recipient_subject': subject,
                'subscription_generation': subscription_generation,
                'endpoint_generation': endpoint_generation,
                'event': event,
            },
            state='pending',
            attempts=0,
            next_attempt_at=app.clock(),
            lease_until=None,
        )
        await tx.enqueue(job)
        return job.id


async def deliver_game_webhook(worker, job):
    """Use the existing worker's lease, retry and honest uncertainty semantics."""
    require(
        job.kind == 'webhook'
        and job.operation == OPERATION
        and isinstance(job.arguments, Mapping)
        and set(job.arguments)
        == {
            'contract_version',
            'recipient_subject',
            'subscription_generation',
            'endpoint_generation',
            'event',
        }
        and type(job.arguments['contract_version']) is int
        and job.arguments['contract_version'] == 1,
        'invalid_game_event',
    )
    event = bounded_game_event(job.arguments['event'])
    require(event['id'] == job.event_id, 'invalid_game_event')
    subject = job.arguments['recipient_subject']
    async with worker.app.metadata.transaction(write=False) as tx:
        _, subscription, endpoint = await _authority(
            worker.app,
            tx,
            subject,
            job.arguments['subscription_generation'],
            job.arguments['endpoint_generation'],
            captured=job.principal,
        )
        require(event['type'] in subscription.get('events', ()), 'game_event_not_subscribed')
        url = endpoint[0]
        secret = open_secret(worker.app, subject, endpoint[1], endpoint[2])
    timestamp = str(int(worker.app.clock().timestamp()))
    body = canonical({
        'event_id': job.event_id,
        'delivery_id': job.id,
        'timestamp': timestamp,
        'subject_id': subject,
        'type': event['type'],
        'event': event,
    })
    state = await worker.webhook_sender.send(
        url, secret, body, timestamp=timestamp, event_id=job.event_id, delivery_id=job.id
    )
    require(state in {'delivered', 'retry', 'failed'}, 'invalid_delivery_result')
    if state == 'retry':
        await worker._retry_webhook(job)
    else:
        await worker._finish(job, 'done' if state == 'delivered' else 'failed', state)
