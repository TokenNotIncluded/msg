"""Domain Event webhook opt-in, current authorization and atomic outbox."""

import json
import secrets

import pytest
from test_authorization import approve, scoped
from test_service import call, register

from msg.core.codec import b64, wire
from msg.core.errors import Failure
from msg.workers.effects import EffectWorker


class Sink:
    def __init__(self):
        self.calls = []

    async def send(self, url, secret, body, **headers):
        self.calls.append(json.loads(body))
        return 'delivered'


async def setup_subscription(app, root):
    key, owner, cert = await register(app, 'domain-owner')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'domain-topic'},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert topic.status == 'ok', wire(topic)
    rid = topic.resources[0].id
    endpoint = await call(
        app,
        'communication.webhook_set',
        {'url': 'https://hooks.example.org/events', 'secret': b64(secrets.token_bytes(32))},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert endpoint.status == 'ok', wire(endpoint)
    requested = {'resource_id': rid, 'events': ['resource.created']}
    denied = await call(
        app, 'communication.webhook_subscribe', requested, key=key, subject=owner, certs=(cert,)
    )
    assert denied.status == 'error' and denied.error.code == 'capability_required', wire(denied)
    domain = await approve(
        app,
        root,
        owner,
        key,
        (scoped(app, 'webhook.domain', rid, ('communication.webhook_subscribe@1',)),),
    )
    subscription = await call(
        app,
        'communication.webhook_subscribe',
        requested,
        key=key,
        subject=owner,
        certs=(cert, domain.resource_id),
    )
    assert subscription.status == 'ok', wire(subscription)
    return key, owner, cert, rid, domain.resource_id


@pytest.mark.asyncio
async def test_domain_webhook_explicit_scope_dedupe_read_is_pure_and_payload_is_bounded(installed):
    app, root = installed
    key, owner, cert, topic, _ = await setup_subscription(app, root)
    before = await call(
        app,
        'communication.webhook_subscription',
        {'resource_id': topic},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert before.status == 'ok' and before.data['enabled']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook'")[0] == 0
    args = {'parent': topic, 'body': 'private words must not leave the service'}
    created = await call(
        app,
        'content.post_create',
        args,
        key=key,
        subject=owner,
        certs=(cert,),
        rid='domain-event-once',
    )
    replay = await call(
        app,
        'content.post_create',
        args,
        key=key,
        subject=owner,
        certs=(cert,),
        rid='domain-event-once',
    )
    assert created.status == 'ok' and replay.replayed
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook'")[0] == 1
    sink = Sink()
    await EffectWorker(app, webhook_sender=sink).run_once()
    assert len(sink.calls) == 1
    payload = sink.calls[0]
    assert set(payload) == {
        'event_id',
        'delivery_id',
        'timestamp',
        'subject_id',
        'type',
        'resource_id',
    }
    assert payload['resource_id'] == created.resources[0].id
    assert payload['type'] == 'resource.created'
    assert 'private words' not in json.dumps(payload)


@pytest.mark.asyncio
async def test_domain_webhook_unsubscribe_and_endpoint_change_stop_pending_delivery(installed):
    app, root = installed
    key, owner, cert, topic, domain = await setup_subscription(app, root)
    first = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'one'},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert first.status == 'ok'
    removed = await call(
        app,
        'communication.webhook_unsubscribe',
        {'resource_id': topic},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert removed.status == 'ok'
    sink = Sink()
    await EffectWorker(app, webhook_sender=sink).run_once()
    assert sink.calls == []
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook' AND state='failed'")[0] == 1
    enabled = await call(
        app,
        'communication.webhook_subscribe',
        {'resource_id': topic, 'events': ['resource.created']},
        key=key,
        subject=owner,
        certs=(cert, domain),
    )
    assert enabled.status == 'ok'
    second = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'two'},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert second.status == 'ok'
    rotated = await call(
        app,
        'communication.webhook_set',
        {'url': 'https://hooks.example.org/new', 'secret': b64(secrets.token_bytes(32))},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert rotated.status == 'ok'
    await EffectWorker(app, webhook_sender=sink).run_once()
    assert sink.calls == []


@pytest.mark.asyncio
async def test_domain_webhook_event_and_job_rollback_together(installed, monkeypatch):
    app, root = installed
    key, owner, cert, topic, _ = await setup_subscription(app, root)
    from msg.plugins import communication

    original = communication.enqueue_domain_webhooks

    async def fail_after_enqueue(app, tx, event):
        await original(app, tx, event)
        if event.type == 'content.post_create':
            raise Failure('forced_projection_failure')

    monkeypatch.setattr(communication, 'enqueue_domain_webhooks', fail_after_enqueue)
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in ('events', 'jobs', 'resources')
        )
    failed = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'rolled back'},
        key=key,
        subject=owner,
        certs=(cert,),
        rid='domain-rollback',
    )
    assert failed.status == 'error' and failed.error.code == 'forced_projection_failure'
    async with app.metadata.transaction(write=False) as tx:
        after = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in ('events', 'jobs', 'resources')
        )
        assert after == before


@pytest.mark.asyncio
async def test_domain_webhook_acl_revocation_stops_queued_delivery(installed):
    app, root = installed
    key, owner, cert, topic, _ = await setup_subscription(app, root)
    created = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'never delivered'},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert created.status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(topic)).generation
    closed = await call(
        app,
        'content.chmod',
        {'id': topic, 'mode': '0000'},
        key=key,
        subject=owner,
        certs=(cert,),
        expected=((topic, generation),),
    )
    assert closed.status == 'ok', closed.error.code if closed.error else wire(closed)
    sink = Sink()
    await EffectWorker(app, webhook_sender=sink).run_once()
    assert sink.calls == []
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook' AND state='failed'")[0] == 1


@pytest.mark.asyncio
async def test_domain_webhook_capability_revocation_stops_pending_and_unsubscribe_still_works(
    installed,
):
    app, root = installed
    key, owner, cert, topic, domain = await setup_subscription(app, root)
    created = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'queued before revoke'},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert created.status == 'ok'
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE certificates SET revoked=1 WHERE id=?', (domain,), write=True)
    sink = Sink()
    await EffectWorker(app, webhook_sender=sink).run_once()
    assert sink.calls == []
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook' AND state='failed'")[0] == 1
    stopped = await call(
        app,
        'communication.webhook_unsubscribe',
        {'resource_id': topic},
        key=key,
        subject=owner,
        certs=(cert,),
    )
    assert stopped.status == 'ok' and stopped.data['enabled'] is False


@pytest.mark.asyncio
async def test_domain_webhook_rejects_other_owner_scope(installed):
    app, root = installed
    key, owner, cert, topic, _ = await setup_subscription(app, root)
    other_key, other, other_cert = await register(app, 'domain-other')
    denied = await call(
        app,
        'communication.webhook_subscribe',
        {'resource_id': topic, 'events': ['resource.created']},
        key=other_key,
        subject=other,
        certs=(other_cert,),
    )
    assert denied.status == 'error' and denied.error.code == 'webhook_scope_not_owned'
