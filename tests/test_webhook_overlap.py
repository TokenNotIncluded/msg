"""Overlapping subscriptions select one currently valid source, then pin it."""

import secrets
from dataclasses import replace

import pytest
from test_authorization import approve, scoped
from test_service import call, register
from test_webhook_domain_events import Sink, setup_subscription

from msg.core.codec import b64, decode, loads
from msg.core.models import Event
from msg.plugins.communication import enqueue_domain_webhooks
from msg.workers.effects import EffectWorker


async def setup_overlap(app, root):
    key, owner, cert, topic, parent_cert = await setup_subscription(app, root)
    post = await call(
        app, 'content.post_create', {'parent': topic, 'body': 'Base'}, key=key, subject=owner
    )
    rid = post.resources[0].id
    # The creation notification predates overlapping update subscriptions.
    await EffectWorker(app, webhook_sender=Sink()).run_once()
    child_cert = await approve(
        app,
        root,
        owner,
        key,
        (scoped(app, 'webhook.domain', rid, ('communication.webhook_subscribe@1',)),),
    )
    for scope, capability in ((topic, parent_cert), (rid, child_cert.resource_id)):
        result = await call(
            app,
            'communication.webhook_subscribe',
            {'resource_id': scope, 'events': ['resource.updated']},
            key=key,
            subject=owner,
            certs=(cert, capability),
        )
        assert result.status == 'ok', result.error
    return key, owner, cert, topic, post, child_cert.resource_id


async def edit(app, key, owner, post, text='New'):
    result = await call(
        app,
        'content.post_edit',
        {'id': post.resources[0].id, 'expected_revision': post.resources[0].revision, 'body': text},
        key=key,
        subject=owner,
        expected=((post.resources[0].id, post.data['generation']),),
    )
    assert result.status == 'ok', result.error
    return result


async def update_jobs(app):
    async with app.metadata.transaction(write=False) as tx:
        return [
            await tx.job(row[0])
            for row in tx.rows("SELECT id FROM jobs WHERE kind='webhook'")
            if (await tx.job(row[0])).arguments.get('category') == 'resource.updated'
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unsubscribe', 'endpoint'])
async def test_overlap_one_job_per_event_recipient_channel_and_pinned_revocation(installed, change):
    app, root = installed
    key, owner, cert, topic, post, _ = await setup_overlap(app, root)
    changed = await edit(app, key, owner, post)
    jobs = await update_jobs(app)
    assert len(jobs) == 1
    chosen = jobs[0]
    assert chosen.arguments['scope_id'] == post.resources[0].id
    # An upgrade must also recognize jobs using the published scope-bearing key.
    async with app.metadata.transaction(write=True) as tx:
        old_key = f'{chosen.event_id}:{owner}:{chosen.arguments["scope_id"]}:{post.resources[0].id}:resource.updated:webhook'
        legacy = replace(chosen, dedupe_key=old_key)
        from msg.core.codec import canonical

        tx.execute(
            'UPDATE jobs SET dedupe=?,body=? WHERE id=?',
            (old_key, canonical(legacy).decode(), chosen.id),
            write=True,
        )
    # Reprojecting the same committed event cannot create another delivery.
    async with app.metadata.transaction(write=True) as tx:
        event = decode(
            Event, loads(tx.one('SELECT body FROM events WHERE id=?', (chosen.event_id,))[0])
        )
        await enqueue_domain_webhooks(app, tx, event)
    assert len(await update_jobs(app)) == 1
    if change == 'unsubscribe':
        stopped = await call(
            app,
            'communication.webhook_unsubscribe',
            {'resource_id': post.resources[0].id},
            key=key,
            subject=owner,
        )
    else:
        stopped = await call(
            app,
            'communication.webhook_set',
            {
                'url': 'https://replacement.example.org/events',
                'secret': b64(secrets.token_bytes(32)),
            },
            key=key,
            subject=owner,
        )
        parent_cert = await approve(
            app,
            root,
            owner,
            key,
            (scoped(app, 'webhook.domain', topic, ('communication.webhook_subscribe@1',)),),
        )
        subscribed = await call(
            app,
            'communication.webhook_subscribe',
            {'resource_id': topic, 'events': ['resource.updated']},
            key=key,
            subject=owner,
            certs=(cert, parent_cert.resource_id),
        )
        assert subscribed.status == 'ok', subscribed.error
    assert stopped.status == 'ok'
    async with app.metadata.transaction(write=True) as tx:
        await enqueue_domain_webhooks(app, tx, event)
    assert len(await update_jobs(app)) == 1
    sink = Sink()
    await EffectWorker(app, webhook_sender=sink).run_once()
    assert sink.calls == []
    # A later event is independent and may select the still-enabled parent.
    await edit(app, key, owner, changed, 'Next event')
    jobs = await update_jobs(app)
    assert len(jobs) == 2 and len({j.event_id for j in jobs}) == 2
    assert next(j for j in jobs if j.id != chosen.id).arguments['scope_id'] == topic
    await EffectWorker(app, webhook_sender=sink).run_once()
    assert len(sink.calls) == 1


@pytest.mark.asyncio
async def test_revoked_child_authority_does_not_suppress_valid_parent(installed):
    app, root = installed
    key, owner, _, topic, post, child_cert = await setup_overlap(app, root)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE certificates SET revoked=1 WHERE id=?', (child_cert,), write=True)
    await edit(app, key, owner, post)
    jobs = await update_jobs(app)
    assert len(jobs) == 1 and jobs[0].arguments['scope_id'] == topic
    sink = Sink()
    await EffectWorker(app, webhook_sender=sink).run_once()
    assert len(sink.calls) == 1


@pytest.mark.asyncio
async def test_different_recipients_and_channels_remain_distinct(installed):
    app, root = installed
    _key, owner, _cert, topic, _, _ = await setup_overlap(app, root)
    other_key, other, other_cert = await register(app, 'overlap-second-owner')
    endpoint = await call(
        app,
        'communication.webhook_set',
        {'url': 'https://other.example.org/events', 'secret': b64(secrets.token_bytes(32))},
        key=other_key,
        subject=other,
    )
    assert endpoint.status == 'ok', endpoint.error
    post = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'Other owner'},
        key=other_key,
        subject=other,
    )
    assert post.status == 'ok', post.error
    target = post.resources[0].id
    capability = await approve(
        app,
        root,
        other,
        other_key,
        (scoped(app, 'webhook.domain', target, ('communication.webhook_subscribe@1',)),),
    )
    result = await call(
        app,
        'communication.webhook_subscribe',
        {'resource_id': target, 'events': ['resource.updated']},
        key=other_key,
        subject=other,
        certs=(other_cert, capability.resource_id),
    )
    assert result.status == 'ok', result.error
    await edit(app, other_key, other, post)
    jobs = await update_jobs(app)
    assert len(jobs) == 2
    assert {job.arguments['recipient_subject'] for job in jobs} == {owner, other}
    # A durable job on another channel cannot suppress this event's webhook.
    chosen = next(job for job in jobs if job.arguments['recipient_subject'] == owner)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('DELETE FROM jobs WHERE id=?', (chosen.id,), write=True)
        mail = replace(
            chosen,
            id='job_other_channel',
            kind='mail',
            dedupe_key=chosen.dedupe_key.removesuffix(':webhook') + ':mail',
        )
        await tx.enqueue(mail)
        event = decode(
            Event, loads(tx.one('SELECT body FROM events WHERE id=?', (chosen.event_id,))[0])
        )
        await enqueue_domain_webhooks(app, tx, event)
        assert tx.one('SELECT COUNT(*) FROM jobs WHERE dedupe=?', (mail.dedupe_key,))[0] == 1
    assert len(await update_jobs(app)) == 2
