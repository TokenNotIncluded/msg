"""Both notification channels share retry timing without weakening lease fencing."""
from dataclasses import replace
from datetime import timedelta

import pytest

from msg.core.models import EffectJob, Principal
from msg.workers.effects import EffectWorker
from test_service import NOW


def job_for(channel, *, attempts=1, state='running'):
    return EffectJob(id='job_retry_state', event_id='e_retry_state', kind=channel,
        dedupe_key='retry-state', principal=Principal(actor=None, subject=None,
        credential_id=None, method='anonymous', certificates=(), ceiling=()),
        operation='communication.send', arguments={}, state=state, attempts=attempts,
        next_attempt_at=NOW-timedelta(seconds=10), lease_until=NOW+timedelta(seconds=120))


async def retry(worker, job):
    if job.kind == 'mail':
        await worker._retry_mail(job, 'smtp_temporary')
    else:
        await worker._retry_webhook(job)


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['mail', 'webhook'])
@pytest.mark.parametrize('attempts', range(1, 10))
async def test_retry_delay_attempt_limit_and_channel_codes_survive_refactor(installed, channel, attempts):
    app, _ = installed
    job = job_for(channel, attempts=attempts)
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)
    await retry(EffectWorker(app), job)
    async with app.metadata.transaction(write=False) as tx:
        current = await tx.job(job.id)
        code = tx.setting('job_status:' + job.id)['code']
    exhausted = attempts >= 8
    assert current == replace(job, state='failed' if exhausted else 'pending', lease_until=None,
        next_attempt_at=job.next_attempt_at if exhausted else NOW+timedelta(seconds=min(3600, 30*2**(attempts-1))))
    assert code == ('smtp_temporary' if channel == 'mail' else
                    'webhook_attempts_exhausted' if exhausted else 'webhook_retry_scheduled')
    # A second process/late completion cannot reschedule this attempt again.
    await retry(EffectWorker(app), job)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.job(job.id) == current
        assert tx.setting('job_status:' + job.id)['code'] == code


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['mail', 'webhook'])
@pytest.mark.parametrize('state', ['pending', 'done', 'failed', 'uncertain', 'new_attempt'])
async def test_late_retry_cannot_overwrite_another_lease_or_terminal_state(installed, channel, state):
    app, _ = installed
    stale = job_for(channel)
    current = replace(stale, state='running', attempts=2) if state == 'new_attempt' else replace(stale, state=state)
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(current)
        tx.set_setting('job_status:' + stale.id, {'code': 'existing_decision'})
    await retry(EffectWorker(app), stale)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.job(stale.id) == current
        assert tx.setting('job_status:' + stale.id) == {'code': 'existing_decision'}


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['mail', 'webhook'])
async def test_expired_external_execution_is_uncertain_not_automatically_retried(installed, channel):
    app, _ = installed
    stale = replace(job_for(channel), lease_until=NOW)
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(stale)
    claimed, run = await EffectWorker(app)._claim()
    assert claimed.id == stale.id and run is False
    await retry(EffectWorker(app), stale)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(stale.id)).state == 'uncertain'
        assert tx.setting('job_status:' + stale.id) == {'code': 'expired_execution_lease'}
