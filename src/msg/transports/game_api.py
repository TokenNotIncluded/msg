"""Bounded process-local machine admissions and opt-in game notifications.

Admission results contain public IDs, never the client nonce which completes
the ticket. Game events enter memory without I/O; a separate consumer performs
current subscription, credential and endpoint checks before durable enqueue.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from uuid import uuid4

from msg.core.codec import unb64
from msg.core.errors import Failure, require
from msg.core.models import Principal
from msg.security.quarantine import require_live_authority
from msg.workers.effects import current_principal

JOIN_OPERATION = 'communication.game_join_ticket'
SUBSCRIBE_OPERATION = 'communication.game_webhook_subscribe'
GAME_EVENTS = frozenset({'game.joined', 'game.left', 'game.region', 'game.collect', 'game.hit'})
TICKET_SECONDS = 30
MAX_TICKETS = 256
MAX_OWNER_TICKETS = 4
MAX_EVENT_QUEUE = 256
MAX_SUBSCRIPTIONS = 256


def subscription_key(subject):
    return 'game_webhook:' + subject


class GameRuntime:
    def __init__(self, service, *, clock=time.monotonic):
        self.service = service
        self.clock = clock
        self.tickets = {}
        self.subscriptions = OrderedDict()
        self.queue = asyncio.Queue(maxsize=MAX_EVENT_QUEUE)
        self.consumer = None
        self.closed = False
        self.dropped = 0
        self.last_world = None
        self.last_sequence = 0

    def issue(self, principal, nonce):
        require(not self.closed, 'server_busy')
        raw = unb64(nonce, limit=32)
        require(len(raw) == 32, 'invalid_request')
        now = self.clock()
        self.tickets = {key: value for key, value in self.tickets.items() if value[0] > now}
        require(len(self.tickets) < MAX_TICKETS, 'server_busy')
        require(
            sum(value[1].subject == principal.subject for value in self.tickets.values())
            < MAX_OWNER_TICKETS,
            'server_busy',
        )
        identifier = 'gt_' + secrets.token_hex(16)
        self.tickets[identifier] = (now + TICKET_SECONDS, principal, hashlib.sha256(raw).digest())
        return {
            'ticket_id': identifier,
            'expires_in': TICKET_SECONDS,
            'websocket': '/_flight',
            'purpose': 'pilot',
            'protocol': 1,
        }

    def take(self, packet):
        require(not self.closed, 'invalid_grant')
        require(type(packet) is dict and set(packet) == {'id', 'nonce'}, 'invalid_grant')
        require(type(packet['id']) is str and type(packet['nonce']) is str, 'invalid_grant')
        require(len(packet['id']) <= 64 and len(packet['nonce']) == 43, 'invalid_grant')
        try:
            nonce = unb64(packet['nonce'], limit=32)
        except Failure, ValueError, TypeError:
            raise Failure('invalid_grant') from None
        record = self.tickets.get(packet['id'])
        require(
            record is not None
            and len(nonce) == 32
            and hmac.compare_digest(record[2], hashlib.sha256(nonce).digest()),
            'invalid_grant',
        )
        # One successful proof attempt consumes the admission before async work.
        del self.tickets[packet['id']]
        require(record[0] > self.clock(), 'invalid_grant')
        return record[1]

    def configure(self, subject, record):
        if not record or not record.get('enabled'):
            self.subscriptions.pop(subject, None)
            return
        self.subscriptions[subject] = {
            'events': frozenset(record['events']),
            'generation': record['generation'],
            'endpoint_generation': record['endpoint_generation'],
        }
        self.subscriptions.move_to_end(subject)
        while len(self.subscriptions) > MAX_SUBSCRIPTIONS:
            self.subscriptions.popitem(last=False)

    def emit(self, subject, category, payload):
        record = self.subscriptions.get(subject)
        if self.closed or not record or category not in record['events']:
            return False
        ship_id = payload.get('ship_id')
        if type(ship_id) is not str or not 1 <= len(ship_id) <= 128:
            return False
        event = {
            'id': 'ge_' + uuid4().hex,
            'type': category,
            'ship_id': ship_id,
            'time_ms': int(time.time() * 1000),
        }
        for field, maximum in [
            ('region', 18),
            ('score', 1_000_000_000),
            ('collected', 1_000_000_000),
            ('hp', 100),
        ]:
            value = payload.get(field)
            if type(value) is int and 0 <= value <= maximum:
                event[field] = value
        try:
            self.queue.put_nowait((
                subject,
                event,
                record['generation'],
                record['endpoint_generation'],
            ))
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        if self.consumer is None or self.consumer.done():
            self.consumer = asyncio.create_task(self._consume(), name='game-event-enqueue')
        return True

    async def _consume(self):
        from msg.workers.game_webhook import enqueue_game_event

        while not self.closed:
            item = await self.queue.get()
            try:
                await enqueue_game_event(self.service, *item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Invalidated authority/capacity or an unavailable DB drops this
                # best-effort pre-durable notification; no data enters logs.
                self.dropped += 1
            finally:
                self.queue.task_done()

    async def close(self):
        self.closed = True
        self.tickets.clear()
        self.subscriptions.clear()
        if self.consumer:
            self.consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.consumer
            self.consumer = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()


def game_runtime(service):
    runtime = getattr(service, '_game_runtime', None)
    if runtime is None:
        runtime = service._game_runtime = GameRuntime(service)
    return runtime


async def validate_machine(service, captured, tx):
    service.runtime_generation.require_current(tx)
    require_live_authority(tx)
    require(not service.executor.recovery_drill_active(), 'recovery_quarantined')
    require(
        isinstance(captured, Principal)
        and captured.method == 'signature'
        and captured.subject is not None
        and captured.actor == captured.subject,
        'invalid_grant',
    )
    principal = await current_principal(service, captured, tx)
    await service.authorizer.require_base(principal, JOIN_OPERATION + '@1', principal.subject, tx)
    resource = await tx.resource(principal.subject)
    subject = await tx.subject(principal.subject)
    require(
        resource.type == 'user' and resource.state == 'active' and not subject.local_only,
        'invalid_grant',
    )
    return principal


async def consume_join_ticket(service, ticket):
    runtime = game_runtime(service)
    captured = runtime.take(ticket)
    async with service.metadata.transaction(write=False) as tx:
        principal = await validate_machine(service, captured, tx)
        runtime.configure(principal.subject, tx.setting(subscription_key(principal.subject)))
    return principal


def emit_game_event(service, subject, event, payload):
    """Trusted producer boundary: never raise into game simulation or teardown."""
    try:
        runtime = getattr(service, '_game_runtime', None)
        return bool(
            runtime
            and subject
            and type(payload) is dict
            and event in GAME_EVENTS
            and runtime.emit(subject, event, payload)
        )
    except Exception:
        return False


def capture_game_events(service, hub):
    """Project only participants' own events, O(max peers + last 64 events)."""
    try:
        runtime = getattr(service, '_game_runtime', None)
        if runtime is None or runtime.closed or not runtime.subscriptions:
            return
        world = hub.world
        current = world._event_id
        if runtime.last_world is not world or current < runtime.last_sequence:
            runtime.last_world, runtime.last_sequence = world, 0
        previous, runtime.last_sequence = runtime.last_sequence, current
        owners = {
            peer.ship_id: peer.subject
            for peer in hub.peers.values()
            if getattr(peer, 'machine_principal', None) is not None
            and peer.ship_id in world.active
            and peer.subject
        }
        for _, _, event in list(world._events)[-64:]:
            if int(event['id'][4:]) <= previous or event['type'] not in {
                'region',
                'collect',
                'hit',
            }:
                continue
            involved = {event['player_id']}
            if event['type'] == 'hit':
                involved.add(event.get('target_id'))
            for ship_id in involved & owners.keys():
                ship = world.ships[ship_id]
                emit_game_event(
                    service,
                    owners[ship_id],
                    'game.' + event['type'],
                    {
                        'ship_id': ship_id,
                        'region': ship.region,
                        'score': ship.score,
                        'collected': ship.collected,
                        'hp': ship.hp,
                    },
                )
    except Exception:
        return


async def close_game_api(service):
    runtime = getattr(service, '_game_runtime', None)
    if runtime is not None:
        await runtime.close()
