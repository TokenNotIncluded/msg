"""Every round is bound to its nonce/deadline/context; final issue is unique."""

import asyncio
from datetime import timedelta

import pytest
from read_only_evidence import business_snapshot
from test_achievements import advance
from test_service import NOW, call, register

from msg.core.codec import loads, wire
from msg.plugins.achievements import FINAL_STATEMENT


def confirmation(challenge):
    result = {
        name: challenge[name]
        for name in ('challenge_id', 'question_digest', 'nonce', 'ceremony_digest')
    }
    result['statement'] = FINAL_STATEMENT
    return result


async def challenge_at(app, key, subject, number):
    started = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert started.status == 'ok', wire(started)
    challenge = started.data
    nonces = {challenge['nonce']}
    for _ in range(1, number):
        step = await advance(app, key, subject, challenge)
        assert step.status == 'ok', wire(step)
        challenge = step.data
        assert challenge['nonce'] not in nonces
        nonces.add(challenge['nonce'])
    assert challenge['round'] == number
    return challenge


@pytest.mark.asyncio
@pytest.mark.parametrize('number', range(1, 6))
@pytest.mark.parametrize(
    'fault,reason',
    [('nonce', 'nonce_mismatch'), ('question', 'context_mismatch'), ('deadline', 'round_expired')],
)
async def test_each_round_rejects_context_and_can_restart(installed, number, fault, reason):
    app, _ = installed
    key, subject, _ = await register(app, 'honor-context')
    challenge = await challenge_at(app, key, subject, number)
    damaged = dict(challenge)
    if fault == 'nonce':
        damaged['nonce'] = ('A' if challenge['nonce'][0] != 'A' else 'B') + challenge['nonce'][1:]
    elif fault == 'question':
        damaged['question_digest'] = 'sha256:' + '0' * 64
    else:
        # The deadline is inclusive; all previous fixture answers happened NOW.
        app.executor.clock = lambda: NOW + timedelta(seconds=60)
    if number == 5:
        failed = await call(
            app, 'achievement.finish', confirmation(damaged), key=key, subject=subject
        )
    else:
        failed = await advance(app, key, subject, damaged)
    assert failed.status == 'ok' and failed.data['status'] == 'failed', wire(failed)
    assert failed.data['reason'] == reason
    async with app.metadata.transaction(write=False) as tx:
        state = loads(
            tx.one('SELECT body FROM achievement_ceremonies WHERE id=?', (challenge['challenge_id'],))[
                0
            ]
        )
        assert state['status'] == 'failed'
        assert 'nonce_digest' not in state and 'expected_digest' not in state
        assert tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0] == 0
    restarted = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert restarted.status == 'ok' and restarted.data['round'] == 1, wire(restarted)
    assert restarted.data['challenge_id'] != challenge['challenge_id']
    assert restarted.data['nonce'] != challenge['nonce']


@pytest.mark.asyncio
async def test_foreign_subject_cannot_finish_or_consume_owner_ceremony(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'honor-owner-boundary')
    other_key, other, _ = await register(app, 'honor-foreign-boundary')
    challenge = await challenge_at(app, key, subject, 5)
    before = await business_snapshot(app)
    denied = await call(
        app, 'achievement.finish', confirmation(challenge), key=other_key, subject=other
    )
    assert denied.status == 'error' and denied.error.code == 'achievement_ceremony_not_found'
    assert await business_snapshot(app) == before
    finished = await call(
        app, 'achievement.finish', confirmation(challenge), key=key, subject=subject
    )
    assert finished.status == 'ok', wire(finished)


@pytest.mark.asyncio
async def test_concurrent_finish_issues_one_grant_and_replays_only_committed_request(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'honor-final-race')
    challenge = await challenge_at(app, key, subject, 5)
    args = confirmation(challenge)
    request_ids = ('honor-finish-a', 'honor-finish-b')
    results = await asyncio.gather(
        *(call(app, 'achievement.finish', args, key=key, subject=subject, rid=rid) for rid in request_ids)
    )
    assert sorted(result.status for result in results) == ['error', 'ok']
    winning = next(index for index, result in enumerate(results) if result.status == 'ok')
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0] == 1
        audit = [loads(row[0]) for row in tx.rows('SELECT body FROM audit')]
        issued = [item for item in audit if item['event']['type'] == 'achievement.auto.issue']
        assert len(issued) == 1
    before = await business_snapshot(app)
    repeated = await call(
        app, 'achievement.finish', args, key=key, subject=subject, rid=request_ids[winning]
    )
    assert repeated.status == 'ok' and repeated.replayed
    assert repeated.data == results[winning].data
    assert await business_snapshot(app) == before
