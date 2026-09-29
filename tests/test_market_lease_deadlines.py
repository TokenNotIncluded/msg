"""An expired notification lease must converge in both job and order projections."""

from datetime import timedelta

import pytest
from test_market_delivery import Sender, drain, enable_mail, verified_address
from test_market_lifecycle import buy, market
from test_service import call

from msg.core.codec import wire
from msg.plugins.money import _balance
from msg.workers.effects import EffectWorker


@pytest.mark.asyncio
@pytest.mark.parametrize('transition', ['finish', 'retry'])
@pytest.mark.parametrize('disabled', [False, True])
async def test_expired_market_attempt_preserves_money_and_notification_truth(
    installed, transition, disabled
):
    app, root = installed
    enable_mail(app)
    _sk, seller, bk, buyer, listing, _ = await market(app, root)
    await verified_address(app, bk, buyer, 'buyer@example.invalid')
    sender = Sender()
    await drain(app, sender)
    sender.messages.clear()
    bought = await buy(app, bk, buyer, listing, email='buyer@example.invalid')
    assert bought.status == 'ok', wire(bought)
    oid = bought.data['order']['id']
    worker = EffectWorker(app, mail_sender=sender, lease_seconds=1)
    job, execute = await worker._claim()
    assert execute and job.kind == 'market_mail' and job.arguments['order_id'] == oid
    now = app.clock()
    if disabled:
        result = await call(app, 'orders.email_disable', {'order_id': oid}, key=bk, subject=buyer)
        assert result.status == 'ok', wire(result)
    app.clock = lambda: now + timedelta(seconds=1)
    # Exercise completion/retry before any sweeper observes the exact deadline.
    if transition == 'finish':
        await worker._finish(job, 'done', 'sent')
    else:
        await worker._retry_mail(job, 'mail_connection_failed')
    got = await call(app, 'orders.get', {'order_id': oid}, key=bk, subject=buyer)
    assert got.status == 'ok', wire(got)
    assert got.data['order']['email_status'] == ('disabled' if disabled else 'delivered_unknown')
    assert got.data['order']['state'] == 'settled'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(job.id)).state == 'uncertain'
        assert tx.setting('job_status:' + job.id) == {'code': 'expired_execution_lease'}
        assert _balance(tx, buyer) == 15_000_000 and _balance(tx, seller) == 5_000_000
        assert tx.one('SELECT state,claimed_at FROM store_deliveries WHERE order_id=?', (oid,)) == (
            'prepared',
            None,
        )
        assert tx.one('SELECT COUNT(*) FROM order_settlements WHERE order_id=?', (oid,))[0] == 1
    assert await worker._claim() == (None, False)
    await worker._finish(job, 'done', 'sent')
    await worker._retry_mail(job, 'mail_connection_failed')
    after = await call(app, 'orders.get', {'order_id': oid}, key=bk, subject=buyer)
    assert after.data['order']['email_status'] == got.data['order']['email_status']
    assert not sender.messages
