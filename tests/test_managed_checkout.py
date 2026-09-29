"""Digest-bound checkout, actual buyer ACK and minimal email on real PostgreSQL."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_order_paths import _headers
from test_orders import _intent, _sale
from test_service import NOW, call, register

from msg.admin.money import apply_money
from msg.core.codec import b64, canonical, digest, loads, unb64, wire
from msg.core.errors import Failure
from msg.core.models import MailConfig
from msg.market.escrow import INSTANT_POLICY, POLICY, POLICY_DIGEST
from msg.plugins.money import _balance
from msg.transports.http import create_app
from msg.workers.effects import EffectWorker


def ok(result):
    assert result.status == 'ok', wire(result)
    return result


class MailSink:
    def __init__(self, result='sent'):
        self.calls = []
        self.result = result

    async def send(self, job):
        self.calls.append(dict(job.arguments))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def enable_mail(app):
    app.settings = replace(
        app.settings,
        server=replace(
            app.settings.server,
            mail=MailConfig(
                enabled=True,
                host='smtp.invalid',
                port=587,
                tls='starttls',
                sender='msg@example.org',
                credential_file=None,
            ),
        ),
    )


async def verified_email(app, key, buyer, address):
    sink = MailSink()
    ok(await call(app, 'identity.email_set', {'address': address}, key=key, subject=buyer))
    worker = EffectWorker(app, mail_sender=sink)
    for _ in range(8):
        if not await worker.run_once():
            break
    challenges = [m for m in sink.calls if m.get('verification')]
    assert len(challenges) == 1, sink.calls
    token = challenges[0]['text'].rsplit(' ', 1)[1]
    ok(await call(app, 'identity.email_verify', {'token': token}, key=key, subject=buyer))
    return challenges[0]


async def setup_sale(installed, *, policy='escrow-instant-v1', quantity=1):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'instant-buyer')
    listing, package = await _sale(app, seller_key, seller, escrow_policy=policy, quantity=quantity)
    await apply_money(app, root, action='mint', operator='test-console', amount_minor=20_000_000)
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test-console',
        subject_id=buyer,
        amount_minor=20_000_000,
    )
    args = {**_intent(listing), 'package_digest': package['digest'], 'auto_accept': True}
    return app, (seller_key, seller), (buyer_key, buyer), package, args


async def buy(app, identity, args, *, rid='instant-checkout'):
    key, buyer = identity
    return await call(app, 'orders.buy', args, key=key, subject=buyer, rid=rid, contract_version=2)


async def read_delivery(app, identity, order_id):
    key, buyer = identity
    return ok(await call(app, 'delivery.get', {'order_id': order_id}, key=key, subject=buyer)).data[
        'delivery'
    ]


@pytest.mark.asyncio
async def test_auto_checkout_is_atomic_but_does_not_claim_and_ack_never_pays_twice(installed):
    app, seller, buyer, package, args = await setup_sale(installed)
    result = ok(await buy(app, buyer, args))
    order = result.data['order']
    assert order['state'] == 'settled'
    assert result.data['delivery']['state'] == 'prepared'
    assert result.data['decision']['body']['reason'] == 'checkout_accept'
    assert result.data['decision']['body']['policy_digest'] == digest(INSTANT_POLICY)
    assert POLICY_DIGEST == digest(POLICY)
    replay = ok(await buy(app, buyer, args))
    assert replay.replayed and replay.data == result.data
    content = await read_delivery(app, buyer, order['id'])
    assert content['manifest']['text'] == 'msg.lmm.best store selftest'
    assert content['payloads'][0]['data'] == b64(b'delivery-ok\n')
    assert content['package_digest'] == package['digest']
    assert content['state'] == 'prepared' and content['claimed_at'] is None
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, buyer[1]) == 15_000_000
        assert _balance(tx, seller[1]) == 5_000_000
        escrow = tx.one('SELECT escrow_subject FROM store_orders WHERE id=?', (order['id'],))[0]
        assert _balance(tx, escrow) == 0
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE debit_account=?', (escrow,))[0] == 1
        decision, source = tx.one(
            'SELECT body,source_proof FROM order_escrow_decisions WHERE order_id=?', (order['id'],)
        )
        signed = loads(unb64(loads(source)['signed_envelope']))
        assert signed['operation'] == 'orders.buy' and signed['contract_version'] == 2
        assert signed['arguments'] == args and loads(decision)['request_id'] == 'instant-checkout'
        before = tx.one('SELECT COUNT(*) FROM money_ledger')[0]
    acknowledgement = {'order_id': order['id'], 'delivery_digest': content['delivery_digest']}
    claimed = ok(
        await call(
            app, 'delivery.claim', acknowledgement, key=buyer[0], subject=buyer[1], rid='claim'
        )
    )
    assert claimed.data['state'] == 'claimed'
    replay = ok(
        await call(
            app, 'delivery.claim', acknowledgement, key=buyer[0], subject=buyer[1], rid='claim'
        )
    )
    assert replay.replayed
    duplicate = await call(
        app, 'delivery.claim', acknowledgement, key=buyer[0], subject=buyer[1], rid='claim-again'
    )
    assert duplicate.error.code == 'delivery_not_acceptable'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == before
    assert (await read_delivery(app, buyer, order['id']))['claimed_at'] is not None


@pytest.mark.asyncio
async def test_auto_prepare_without_acceptance_keeps_funds_in_escrow(installed):
    app, seller, buyer, _, args = await setup_sale(installed, policy='escrow-v1')
    args['auto_accept'] = False
    result = ok(await buy(app, buyer, args))
    order = result.data['order']
    assert order['state'] == 'delivered' and 'settlement' not in result.data
    content = await read_delivery(app, buyer, order['id'])
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, seller[1]) == 0
        assert tx.one('SELECT COUNT(*) FROM order_escrow_decisions')[0] == 0
    accepted = ok(
        await call(
            app,
            'delivery.accept',
            {'order_id': order['id'], 'delivery_digest': content['delivery_digest']},
            key=buyer[0],
            subject=buyer[1],
        )
    )
    assert accepted.data['decision']['body']['policy_digest'] == POLICY_DIGEST
    assert accepted.data['state'] == 'settled'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['digest', 'legacy-policy', 'email-format'])
async def test_invalid_signed_quote_has_no_payment_delivery_or_mail_effect(installed, failure):
    app, _, buyer, _, args = await setup_sale(
        installed, policy='escrow-v1' if failure == 'legacy-policy' else 'escrow-instant-v1'
    )
    if failure == 'digest':
        args['package_digest'] = 'sha256:' + '0' * 64
    elif failure == 'email-format':
        args['email'] = 'someone@example.org\r\nBcc:other@example.org'
    result = await buy(app, buyer, args)
    assert result.status == 'error', wire(result)
    assert (
        result.error.code
        == {
            'digest': 'delivery_package_mismatch',
            'legacy-policy': 'escrow_reason_unsupported',
            'email-format': 'invalid_email',
        }[failure]
    )
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, buyer[1]) == 20_000_000
        for table in ('store_orders', 'store_deliveries', 'order_escrow_decisions', 'jobs'):
            assert tx.one(f'SELECT COUNT(*) FROM {table}')[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['decision', 'notification'])
async def test_checkout_rolls_back_all_facts_when_a_late_commit_step_fails(
    installed, monkeypatch, stage
):
    app, _, buyer, _, args = await setup_sale(installed)
    if stage == 'notification':
        enable_mail(app)
        await verified_email(app, *buyer, 'buyer@example.org')
        args['email'] = 'buyer@example.org'
    async with app.metadata.transaction(write=False) as tx:
        cls = type(tx)
        before = {
            table: tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('money_ledger', 'events', 'jobs', 'results', 'settings')
        }
    execute = cls.execute

    def fail_late(self, sql, parameters=(), **kwargs):
        if (stage == 'decision' and 'INSERT INTO order_escrow_decisions' in sql) or (
            stage == 'notification' and 'INSERT' in sql and 'jobs' in sql
        ):
            raise Failure('injected_commit_failure')
        return execute(self, sql, parameters, **kwargs)

    monkeypatch.setattr(cls, 'execute', fail_late)
    result = await buy(app, buyer, args)
    assert result.error.code == 'injected_commit_failure', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, buyer[1]) == 20_000_000
        for table, count in before.items():
            assert tx.one(f'SELECT COUNT(*) FROM {table}')[0] == count, table
        for table in ('store_orders', 'store_deliveries', 'order_escrow_decisions'):
            assert tx.one(f'SELECT COUNT(*) FROM {table}')[0] == 0
        assert tx.one("SELECT COUNT(*) FROM ledger_accounts WHERE kind='order_escrow'")[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('same_id', [True, False])
async def test_competing_checkouts_never_double_sell_or_double_release(installed, same_id):
    app, seller, buyer, _, args = await setup_sale(installed)
    responses = await asyncio.gather(
        *(buy(app, buyer, args, rid='same-id' if same_id else f'checkout-{i}') for i in range(2))
    )
    assert sum(r.status == 'ok' and not r.replayed for r in responses) == 1, wire(responses)
    if same_id:
        assert sum(r.replayed for r in responses) == 1
    else:
        assert (
            next(r for r in responses if r.status == 'error').error.code == 'quantity_unavailable'
        )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM store_deliveries')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM order_escrow_decisions')[0] == 1
        assert _balance(tx, seller[1]) == 5_000_000 and _balance(tx, buyer[1]) == 15_000_000


@pytest.mark.asyncio
async def test_smtp_disabled_or_unverified_email_does_not_stop_site_settlement(installed):
    app, _, buyer, _, args = await setup_sale(installed)
    args['email'] = 'not-verified@example.org'
    result = ok(await buy(app, buyer, args))
    order_id = result.data['order']['id']
    assert result.data['order']['state'] == 'settled'
    assert result.data['notification']['state'] == 'disabled'
    enable_mail(app)
    pending = ok(
        await call(app, 'delivery.notify', {'order_id': order_id}, key=buyer[0], subject=buyer[1])
    )
    assert pending.data['notification']['state'] == 'pending'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM jobs')[0] == 0
    # Verification is the existing explicit identity flow, not an implicit
    # checkout overwrite. Its message carries no order or item information.
    challenge = await verified_email(app, *buyer, args['email'])
    assert order_id not in canonical(challenge).decode()
    assert 'instant-buyer' not in challenge['text']
    assert 'store selftest' not in challenge['text']
    queued = ok(
        await call(app, 'delivery.notify', {'order_id': order_id}, key=buyer[0], subject=buyer[1])
    )
    assert queued.data['notification']['state'] == 'queued'
    sink = MailSink()
    assert await EffectWorker(app, mail_sender=sink).run_once()
    assert len(sink.calls) == 1
    content = await read_delivery(app, buyer, order_id)
    assert content['notification']['state'] == 'smtp_accepted'
    assert content['state'] == 'prepared' and content['claimed_at'] is None


@pytest.mark.asyncio
async def test_verified_email_contains_only_order_handle_snapshot_and_reauth_link(installed):
    app, seller, buyer, _, args = await setup_sale(installed)
    enable_mail(app)
    await verified_email(app, *buyer, 'buyer@example.org')
    args['email'] = 'buyer@example.org'
    result = ok(await buy(app, buyer, args))
    order_id = result.data['order']['id']
    assert result.data['notification']['state'] == 'queued'
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(buyer[1])
        original_name = resource.name
        # A later rename/restore cannot silently change the checkout snapshot.
        await tx.replace(
            replace(resource, name='@renamed-buyer', generation=resource.generation + 1),
            resource.generation,
        )
        fact = tx.setting('delivery_notification:' + order_id)
        job = await tx.job(fact['job_id'])
        # Persisted job text is not a second, unchecked mail API.
        await tx.save_job(
            replace(
                job,
                arguments={
                    **job.arguments,
                    'text': 'leaked product',
                    'subject': 'leaked title',
                    'recipient': 'thief@example.org',
                },
            )
        )
    sink = MailSink()
    assert await EffectWorker(app, mail_sender=sink).run_once()
    assert len(sink.calls) == 1
    mail = sink.calls[0]
    link = app.settings.service_url + '/_orders/' + order_id + '/_delivery'
    assert mail == {
        'recipient': 'buyer@example.org',
        'recipient_subject': buyer[1],
        'subject': 'msg order ready',
        'text': order_id + '\n' + original_name + '\n' + link + '\n',
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        anonymous = await http.get(link)
        assert anonymous.status_code in {403, 404}
        authorized = await http.get(
            link, headers=_headers(app, 'delivery.get', {'order_id': order_id}, buyer[0], buyer[1])
        )
        assert authorized.status_code == 200, authorized.text
    seller_view = ok(
        await call(app, 'orders.get', {'order_id': order_id}, key=seller[0], subject=seller[1])
    )
    assert 'buyer@example.org' not in canonical(wire(seller_view)).decode()
    denied = await call(
        app, 'delivery.notify', {'order_id': order_id}, key=seller[0], subject=seller[1]
    )
    assert denied.error.code == 'order_not_found'
    # Neither reads nor another notification request sends a second message.
    ok(await call(app, 'delivery.notify', {'order_id': order_id}, key=buyer[0], subject=buyer[1]))
    assert not await EffectWorker(app, mail_sender=sink).run_once()
    assert (await read_delivery(app, buyer, order_id))['state'] == 'prepared'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'tamper', ['email-owner', 'email-generation', 'target', 'credential', 'preference']
)
async def test_mail_worker_rechecks_current_authority_and_bound_endpoint(installed, tamper):
    app, _, buyer, _, args = await setup_sale(installed)
    enable_mail(app)
    await verified_email(app, *buyer, 'buyer@example.org')
    args['email'] = 'buyer@example.org'
    result = ok(await buy(app, buyer, args))
    order_id = result.data['order']['id']
    if tamper == 'preference':
        ok(
            await call(
                app,
                'delivery.notify',
                {'order_id': order_id, 'enabled': False},
                key=buyer[0],
                subject=buyer[1],
            )
        )
    else:
        async with app.metadata.transaction(write=True) as tx:
            if tamper == 'email-owner':
                email = loads(tx.one('SELECT body FROM emails WHERE subject=?', (buyer[1],))[0])
                email['subject_id'] = 'another-subject'
                tx.execute(
                    'UPDATE emails SET body=? WHERE subject=?',
                    (canonical(email).decode(), buyer[1]),
                    write=True,
                )
            elif tamper == 'email-generation':
                tx.execute(
                    'UPDATE emails SET generation=generation+1 WHERE subject=?',
                    (buyer[1],),
                    write=True,
                )
            elif tamper == 'target':
                tx.execute(
                    'UPDATE store_orders SET delivery_target=? WHERE id=?',
                    (
                        canonical({'subject_id': 'someone-else', 'channel': 'site'}).decode(),
                        order_id,
                    ),
                    write=True,
                )
            else:
                fact = tx.setting('delivery_notification:' + order_id)
                job = await tx.job(fact['job_id'])
                credential = await tx.credential(job.principal.credential_id)
                subject = await tx.subject(buyer[1])
                await tx.save_credential(replace(credential, revoked_at=NOW), subject.auth_version)
    sink = MailSink()
    assert await EffectWorker(app, mail_sender=sink).run_once()
    assert sink.calls == []
    async with app.metadata.transaction(write=False) as tx:
        fact = tx.setting('delivery_notification:' + order_id)
        assert (await tx.job(fact['job_id'])).state == 'failed'
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order_id,))[0] == 'settled'
        assert (
            tx.one('SELECT state FROM store_deliveries WHERE order_id=?', (order_id,))[0]
            == 'prepared'
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['uncertain', 'connection-failure'])
async def test_smtp_failure_never_rolls_back_site_or_blindly_retries_ambiguity(installed, result):
    app, seller, buyer, _, args = await setup_sale(installed)
    enable_mail(app)
    await verified_email(app, *buyer, 'buyer@example.org')
    args['email'] = 'buyer@example.org'
    bought = ok(await buy(app, buyer, args))
    order_id = bought.data['order']['id']
    sink = MailSink(
        'uncertain' if result == 'uncertain' else Failure('mail_connection_failed', retryable=True)
    )
    worker = EffectWorker(app, mail_sender=sink)
    assert await worker.run_once()
    assert not await worker.run_once()
    content = await read_delivery(app, buyer, order_id)
    assert content['state'] == 'prepared' and content['claimed_at'] is None
    assert content['notification']['state'] == (
        'delivered_unknown' if result == 'uncertain' else 'queued'
    )
    ok(await call(app, 'delivery.notify', {'order_id': order_id}, key=buyer[0], subject=buyer[1]))
    assert not await worker.run_once()
    assert len(sink.calls) == 1
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, seller[1]) == 5_000_000 and _balance(tx, buyer[1]) == 15_000_000
        assert tx.one('SELECT COUNT(*) FROM order_escrow_decisions')[0] == 1


@pytest.mark.asyncio
async def test_claim_after_checkout_decision_ttl_still_verifies_the_committed_fact(installed):
    app, _, buyer, _, args = await setup_sale(installed)
    result = ok(await buy(app, buyer, args))
    order_id = result.data['order']['id']
    content = await read_delivery(app, buyer, order_id)
    # Old payment authorization expires; a fresh signed claim is independently
    # authenticated and must not replay or re-execute that payment decision.
    app.executor.clock = lambda: NOW + timedelta(seconds=121)
    from msg.core.requests import request_for

    packet = request_for(
        'delivery.claim',
        {'order_id': order_id, 'delivery_digest': content['delivery_digest']},
        app.settings.service_url,
        signer=buyer[0],
        subject=buyer[1],
        expires_at=NOW + timedelta(seconds=240),
    )
    claimed = ok(await app.executor.execute(packet))
    assert claimed.data['state'] == 'claimed'


@pytest.mark.asyncio
async def test_prefunded_bounty_then_automatic_bundle_market_e2e(installed):
    app, root = installed
    bank_key, bank, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'market-instant-buyer')
    await apply_money(app, root, action='mint', operator='test-console', amount_minor=20_000_000)
    await apply_money(app, root, action='bank_add', operator='test-console', subject_id=bank)
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test-console',
        subject_id=bank,
        amount_minor=20_000_000,
    )
    bounty = ok(
        await call(
            app,
            'bounty.create',
            {
                'name': 'instant-signature-reward',
                'terms': 'One signed challenge.',
                'reward_minor': 10_000_000,
                'budget_minor': 10_000_000,
                'max_claims': 1,
            },
            key=bank_key,
            subject=bank,
        )
    )
    challenge = ok(
        await call(
            app,
            'bounty.challenge',
            {'listing_id': bounty.data['bounty']['listing_id']},
            key=buyer_key,
            subject=buyer,
        )
    )
    payload = challenge.data['challenge']
    claim_args = {
        'challenge_id': payload['challenge_id'],
        'proof': wire(buyer_key.sign(canonical(payload), purpose='bounty-pop-v1')),
    }
    ok(await call(app, 'bounty.claim', claim_args, key=buyer_key, subject=buyer, rid='reward'))
    duplicate = ok(
        await call(app, 'bounty.claim', claim_args, key=buyer_key, subject=buyer, rid='reward')
    )
    assert duplicate.replayed
    listing, package = await _sale(
        app, bank_key, bank, quantity=1, escrow_policy='escrow-instant-v1'
    )
    result = ok(
        await buy(
            app,
            (buyer_key, buyer),
            {**_intent(listing), 'package_digest': package['digest'], 'auto_accept': True},
        )
    )
    assert result.data['order']['state'] == 'settled'
    content = await read_delivery(app, (buyer_key, buyer), result.data['order']['id'])
    assert content['manifest']['text'] == 'msg.lmm.best store selftest'
    assert content['payloads'][0]['data'] == b64(b'delivery-ok\n')
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 20_000_000
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, buyer) == 5_000_000 and _balance(tx, bank) == 15_000_000
        for (account,) in tx.rows(
            "SELECT id FROM ledger_accounts WHERE kind IN ('order_escrow','bounty_escrow')"
        ):
            assert _balance(tx, account) == 0
