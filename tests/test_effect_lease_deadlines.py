"""A late notification worker cannot requeue an expired, unclaimed attempt."""
import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from msg.core.models import EffectJob, Principal
from msg.workers.effects import EffectWorker


def notification(app, channel, deadline):
    now = app.clock()
    lease_until = {'exact': now, 'past': now - timedelta(seconds=1),
                   'missing': None, 'live': now + timedelta(seconds=30)}[deadline]
    return EffectJob(id='job_lease_deadline', event_id='e_lease_deadline', kind=channel,
        dedupe_key='lease-deadline', principal=Principal(actor=None, subject=None,
        credential_id=None, method='anonymous', certificates=(), ceiling=()),
        operation='communication.send', arguments={}, state='running', attempts=1,
        next_attempt_at=now - timedelta(seconds=10), lease_until=lease_until)


async def retry(worker, job):
    if job.kind == 'mail':
        await worker._retry_mail(job, 'mail_connection_failed')
    else:
        await worker._retry_webhook(job)


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['mail', 'webhook'])
@pytest.mark.parametrize('deadline', ['exact', 'past', 'missing'])
@pytest.mark.parametrize('transition', ['retry', 'finish'])
async def test_expired_attempt_is_fenced_without_another_worker_claim(installed, channel, deadline, transition):
    app, _ = installed
    job = notification(app, channel, deadline)
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)
    worker = EffectWorker(app)
    # No _claim sweep runs first. The old worker must enforce the deadline itself.
    if transition == 'retry':
        await retry(worker, job)
    else:
        await worker._finish(job, 'done', 'delivered')
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.job(job.id) == replace(job, state='uncertain', lease_until=None)
        assert tx.setting('job_status:' + job.id) == {'code': 'expired_execution_lease'}
    claimed, execute = await worker._claim()
    assert claimed is None and execute is False
    await retry(worker, job)
    await worker._finish(job, 'done', 'late_completion')
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(job.id)).state == 'uncertain'
        assert tx.setting('job_status:' + job.id) == {'code': 'expired_execution_lease'}


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['mail', 'webhook'])
async def test_concurrent_expired_completion_and_retry_preserve_uncertainty(installed, channel):
    app, _ = installed
    job = notification(app, channel, 'exact')
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)
    first, second = EffectWorker(app), EffectWorker(app)
    await asyncio.gather(retry(first, job), second._finish(job, 'done', 'delivered'))
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(job.id)).state == 'uncertain'
        assert tx.setting('job_status:' + job.id) == {'code': 'expired_execution_lease'}
