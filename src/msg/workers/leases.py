"""One transaction-local fence for publishing any durable effect's result."""
from dataclasses import replace


async def current_attempt(app, tx, job):
    """Return the live attempt, or persist uncertainty without resending work.

    Call within the same write transaction as the protected state/ref update.
    A stale callback must not overwrite another attempt's status, even when no
    separate worker has swept the expired lease yet.
    """
    current = await tx.job(job.id)
    if current.state != 'running' or current.attempts != job.attempts:
        return None
    if current.lease_until is None or current.lease_until <= app.clock():
        await tx.save_job(replace(current, state='uncertain', lease_until=None))
        tx.set_setting('job_status:' + job.id, {'code': 'expired_execution_lease'})
        from msg.market.targets import notification_status
        notification_status(tx, current, 'uncertain', 'expired_execution_lease')
        return None
    return current
