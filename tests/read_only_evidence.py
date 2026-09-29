"""Read acceptance checks values and effect entrypoints, not just row counts."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock


async def business_snapshot(app):
    async with app.metadata.transaction(write=False) as tx:
        names = [
            row[0]
            for row in tx.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
        ]
        return {
            name: sorted(
                row[0]
                for row in tx.execute(
                    'SELECT row_to_json(t)::text FROM "' + name.replace('"', '""') + '" AS t'
                )
            )
            for name in names
        }


@asynccontextmanager
async def readonly_evidence(app, monkeypatch):
    """Block normal publication/outbound interfaces during the real read call.

    Database snapshots additionally detect direct SQL updates, including ones
    which leave every row count unchanged. No production path bypass is installed.
    """
    from msg.workers import maintenance
    from msg.workers.effects import EffectWorker
    from msg.workers.mail import SmtpSender
    from msg.workers.sandbox import BubblewrapRunner
    from msg.workers.webhook import WebhookSender

    before = await business_snapshot(app)

    message = 'read attempted publication, maintenance or an external effect'
    spies = []

    with monkeypatch.context() as patch:

        def intercept(target, name):
            spy = AsyncMock(side_effect=AssertionError(message))
            patch.setattr(target, name, spy)
            spies.append(spy)

        for name in ('put', 'put_bytes', 'pin', 'unpin', 'commit_revision'):
            intercept(app.contents, name)
        for target, name in (
            (SmtpSender, 'send'),
            (WebhookSender, 'send'),
            (BubblewrapRunner, '__call__'),
            (EffectWorker, 'run_once'),
            (maintenance, 'run_maintenance'),
        ):
            intercept(target, name)
        if app.metadata.signal is not None:
            intercept(app.metadata.signal, 'publish_pending')
        yield
        # Post-commit signal errors may be caught by the store. An attempted
        # effect still invalidates read evidence, even when the caller recovers.
        assert not any(spy.called for spy in spies), message
    assert await business_snapshot(app) == before, 'read changed authoritative row values'
