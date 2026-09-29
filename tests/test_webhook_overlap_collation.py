"""Repeat delivery invariants with linguistic PostgreSQL ordering, as in CI."""
import pytest
from test_webhook_overlap import (
    test_different_recipients_and_channels_remain_distinct as exercise_channels,
)
from test_webhook_overlap import (
    test_overlap_one_job_per_event_recipient_channel_and_pinned_revocation as exercise_overlap,
)


@pytest.fixture
async def english_jobs(installed):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('ALTER TABLE jobs ALTER COLUMN dedupe TYPE TEXT COLLATE "en-US-x-icu"', write=True)
        # Unlike C ordering, punctuation is not an event-prefix boundary here.
        assert tx.one("SELECT 'e_abc:recipient' COLLATE \"en-US-x-icu\" < 'e_abc;'")[0] is False
    return installed


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unsubscribe', 'endpoint'])
async def test_overlap_dedupe_survives_english_collation(english_jobs, change):
    await exercise_overlap(english_jobs, change)


@pytest.mark.asyncio
async def test_channel_isolation_survives_english_collation(english_jobs):
    await exercise_channels(english_jobs)
