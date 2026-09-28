"""Shared notification boundaries, with job facts separate from goods/settlement."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.core.models import EffectJob, Principal
from msg.market import targets
from msg.market import delivery_targets

NOW = datetime(2026, 9, 27, tzinfo=UTC)
BUYER, ORDER, ENDPOINT = 'u_buyer', 'ord_notification', 'em_current'


def app_for(origin='https://msg.example.invalid', enabled=True):
    return SimpleNamespace(settings=SimpleNamespace(service_url=origin,
        server=SimpleNamespace(mail=SimpleNamespace(enabled=enabled))))


class ReadSession:
    """Unit-only read adapter; integration cases below use the real application."""
    def __init__(self, job=None, code=None):
        self.job = job
        self.code = code
        self.reads = []

    async def resource(self, subject):
        assert subject == BUYER
        return SimpleNamespace(name='@buyer')

    def one(self, sql, parameters=()):
        self.reads.append((sql, parameters))
        if 'FROM jobs' in sql:
            assert parameters == ('market-mail:' + ORDER + ':' + ENDPOINT,)
            return (canonical(self.job).decode(),) if self.job else None
        return None

    def setting(self, name, default=None):
        assert name == 'job_status:' + self.job.id
        return {'code': self.code} if self.code is not None else default


def order_for(*, state='queued', disabled=False):
    return {'id': ORDER, 'buyer': BUYER,
        'delivery_target': {'subject_id': BUYER, 'channel': 'site',
            'email': {'subject_id': BUYER, 'endpoint_id': ENDPOINT,
                'address_snapshot': 'buyer@example.invalid', 'state': state,
                **({'notification_disabled': True} if disabled else {})}}}


def job_for(state, *, attempts=1, subject=BUYER, endpoint=ENDPOINT):
    principal = Principal(actor=subject, subject=subject, credential_id=None,
        method='signature', certificates=(), ceiling=())
    return EffectJob(id='job_notice', event_id='delivery:' + ORDER, kind='market_mail',
        dedupe_key='market-mail:' + ORDER + ':' + endpoint, principal=principal,
        operation='orders.buy', arguments={'order_id': ORDER, 'endpoint_id': endpoint},
        state=state, attempts=attempts, next_attempt_at=NOW,
        lease_until=NOW + timedelta(seconds=120) if state == 'running' else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('codepoint', list(range(32)) + [127])
async def test_unit_checkout_rejects_control_characters_like_legacy(codepoint):
    address = 'buyer' + chr(codepoint) + '@example.invalid'
    with pytest.raises(Failure) as legacy:
        delivery_targets.validate_address(address)
    assert legacy.value.code == 'invalid_email'
    with pytest.raises(Failure) as current:
        await targets.target_for(app_for(), ReadSession(), BUYER, address)
    assert current.value.code == 'invalid_email'


@pytest.mark.parametrize('state,code,expected', [
    ('pending', None, 'queued'), ('running', None, 'queued'),
    ('pending', 'mail_connection_failed', 'pending'),
    ('done', 'sent', 'smtp_accepted'), ('done', 'mail_disabled', 'disabled'),
    ('failed', 'credential_expired', 'failed'),
    ('uncertain', 'expired_execution_lease', 'delivered_unknown'),
])
def test_unit_projection_reads_job_not_stale_order_state(state, code, expected):
    order = order_for(state='smtp_accepted')
    before = deepcopy(order)
    tx = ReadSession(job_for(state), code)
    view = targets.notification_view(tx, order)
    assert view['state'] == expected
    assert view['endpoint_id'] == ENDPOINT
    assert order == before  # A GET must not rewrite the order's stored projection.


def test_unit_disabled_endpoint_wins_without_reactivating_old_job():
    tx = ReadSession(job_for('done'), 'sent')
    assert targets.notification_view(tx, order_for(disabled=True))['state'] == 'disabled'
    assert tx.reads == []


def test_unit_missing_job_cannot_claim_sent_from_cached_order():
    assert targets.notification_view(ReadSession(), order_for(state='smtp_accepted'))['state'] == 'pending'


@pytest.mark.parametrize('change', ['subject', 'endpoint', 'order', 'kind'])
def test_unit_projection_rejects_cross_order_or_cross_subject_job(change):
    job = job_for('done')
    if change == 'subject':
        job = job_for('done', subject='u_other')
    elif change == 'endpoint':
        job = job_for('done', endpoint='em_other')
    elif change == 'order':
        job = replace(job, arguments={**job.arguments, 'order_id': 'ord_other'})
    else:
        job = replace(job, kind='market_email_verify')
    with pytest.raises(Failure) as caught:
        targets.notification_view(ReadSession(job, 'sent'), order_for())
    assert caught.value.code == 'delivery_recipient_mismatch'


@pytest.mark.parametrize('origin', [
    'javascript:alert(1)', 'https://name:secret@example.invalid',
    'https://msg.example.invalid?token=secret', 'https://msg.example.invalid#secret',
    'https://', 'https://[broken', 'https://msg.example.invalid\r\nBcc: other',
    'https://msg.example.invalid\x00', 'https://msg.example.invalid\\@other.invalid',
])
def test_unit_pickup_origin_fails_closed_without_echo(origin):
    with pytest.raises(Failure) as caught:
        delivery_targets.pickup_url(app_for(origin), ORDER)
    assert caught.value.code == 'invalid_delivery_origin'
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('origin', ['https://msg.example.invalid',
    'http://localhost:8099', 'https://msg.example.invalid/prefix/'])
def test_unit_pickup_link_preserves_plain_authenticated_route(origin):
    assert delivery_targets.pickup_url(app_for(origin), ORDER) == (
        origin.rstrip('/') + '/_orders/' + ORDER + '/_delivery')


@pytest.mark.asyncio
async def test_integration_market_completion_only_changes_job_facts(installed):
    from test_market_delivery import Sender, drain, enable_mail, verified_address
    from test_market_lifecycle import buy, market
    from test_service import call

    app, root = installed
    enable_mail(app)
    _sk, _seller, key, buyer, listing, _ = await market(app, root)
    await verified_address(app, key, buyer, 'buyer@example.invalid')
    sender = Sender()
    await drain(app, sender)
    sender.messages.clear()
    bought = await buy(app, key, buyer, listing, email='buyer@example.invalid')
    assert bought.status == 'ok', wire(bought)
    oid = bought.data['order']['id']
    tables = ('store_orders', 'store_deliveries', 'money_ledger', 'order_contracts')
    async with app.metadata.transaction(write=False) as tx:
        before = {t: list(tx.execute('SELECT * FROM ' + t + ' ORDER BY 1')) for t in tables}
    await drain(app, sender)
    assert len(sender.messages) == 1
    got = await call(app, 'orders.get', {'order_id': oid}, key=key, subject=buyer)
    assert got.status == 'ok', wire(got)
    assert got.data['order']['email_status'] == 'smtp_accepted'
    assert got.data['order']['delivery_target']['email']['state'] == 'smtp_accepted'
    async with app.metadata.transaction(write=False) as tx:
        assert {t: list(tx.execute('SELECT * FROM ' + t + ' ORDER BY 1')) for t in tables} == before
        assert tx.one('SELECT state,claimed_at FROM store_deliveries WHERE order_id=?', (oid,)) == ('prepared', None)


@pytest.mark.asyncio
async def test_integration_lifecycle_email_uses_same_origin_guard(installed):
    from test_market_delivery import Sender, drain, enable_mail, verified_address
    from test_market_lifecycle import buy, market
    from msg.workers.effects import EffectWorker

    app, root = installed
    enable_mail(app)
    _sk, _seller, key, buyer, listing, _ = await market(app, root)
    await verified_address(app, key, buyer, 'buyer@example.invalid')
    sender = Sender()
    await drain(app, sender)
    sender.messages.clear()
    bought = await buy(app, key, buyer, listing, email='buyer@example.invalid')
    assert bought.status == 'ok', wire(bought)
    worker = EffectWorker(app, mail_sender=sender)
    job, execute = await worker._claim()
    assert execute and job.kind == 'market_mail'
    app.settings = replace(app.settings, service_url='https://secret@example.invalid')
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure) as caught:
            await targets.render_notification(app, tx, job)
        assert caught.value.code == 'invalid_delivery_origin'
    assert sender.messages == []
