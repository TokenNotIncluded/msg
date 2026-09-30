"""Concurrent and retried ceremonies keep one challenge and one honor fact."""

import asyncio

import pytest
from test_achievements import advance
from test_service import call, register

from msg.core.codec import loads, wire
from msg.plugins.achievements import FINAL_STATEMENT


@pytest.mark.asyncio
async def test_concurrent_starts_keep_the_successful_challenge_usable(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'honor-concurrent-start')
    results = await asyncio.gather(
        *(
            call(app, 'achievement.start', {}, key=key, subject=subject, rid=f'start-{index}')
            for index in range(2)
        )
    )
    successes = [result for result in results if result.status == 'ok']
    errors = [result for result in results if result.status == 'error']
    assert len(successes) == len(errors) == 1, [wire(result) for result in results]
    assert errors[0].error.code == 'achievement_ceremony_active'
    async with app.metadata.transaction(write=False) as tx:
        rows = tx.rows('SELECT id,state FROM achievement_ceremonies WHERE subject=?', (subject,))
        assert rows == [(successes[0].data['challenge_id'], 'active')]
    advanced = await advance(app, key, subject, successes[0].data)
    assert advanced.status == 'ok' and advanced.data['round'] == 2, wire(advanced)


@pytest.mark.asyncio
async def test_answer_request_replay_preserves_nonce_without_advancing_again(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'honor-answer-replay')
    challenge = (await call(app, 'achievement.start', {}, key=key, subject=subject)).data
    arguments = {
        name: challenge[name] for name in ('challenge_id', 'round', 'question_digest', 'nonce')
    }
    arguments['answer'] = 'y'
    results = await asyncio.gather(
        *(
            call(app, 'achievement.answer', arguments, key=key, subject=subject, rid='answer-once')
            for _ in range(2)
        )
    )
    assert all(result.status == 'ok' for result in results), [wire(result) for result in results]
    assert sum(result.replayed for result in results) == 1
    assert results[0].data == results[1].data
    assert results[0].data['round'] == 2
    assert results[0].data['nonce'] != challenge['nonce']
    third = await advance(app, key, subject, results[0].data)
    assert third.status == 'ok' and third.data['round'] == 3, wire(third)
    replay = await call(
        app, 'achievement.answer', arguments, key=key, subject=subject, rid='answer-once'
    )
    assert replay.status == 'ok' and replay.replayed
    assert replay.data == results[0].data
    async with app.metadata.transaction(write=False) as tx:
        state = loads(
            tx.one(
                'SELECT body FROM achievement_ceremonies WHERE id=?', (challenge['challenge_id'],)
            )[0]
        )
        assert state['status'] == 'active' and state['round'] == 3
        assert [answer['round'] for answer in state['answers']] == [1, 2]


@pytest.mark.asyncio
async def test_concurrent_finish_and_replay_issue_only_one_grant_and_audit(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'honor-concurrent-finish')
    challenge = (await call(app, 'achievement.start', {}, key=key, subject=subject)).data
    for _ in range(4):
        result = await advance(app, key, subject, challenge)
        assert result.status == 'ok', wire(result)
        challenge = result.data
    arguments = {
        name: challenge[name]
        for name in ('challenge_id', 'question_digest', 'nonce', 'ceremony_digest')
    }
    arguments['statement'] = FINAL_STATEMENT
    results = await asyncio.gather(
        *(
            call(
                app,
                'achievement.finish',
                arguments,
                key=key,
                subject=subject,
                rid=f'finish-{index}',
            )
            for index in range(2)
        )
    )
    successful = [index for index, result in enumerate(results) if result.status == 'ok']
    assert len(successful) == 1, [wire(result) for result in results]
    winner = successful[0]
    assert results[1 - winner].error.code == 'ceremony_inactive'
    replay = await call(
        app,
        'achievement.finish',
        arguments,
        key=key,
        subject=subject,
        rid=f'finish-{winner}',
    )
    assert replay.status == 'ok' and replay.replayed
    assert replay.data == results[winner].data
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0] == 1
        audits = [loads(row[0]) for row in tx.rows('SELECT body FROM audit')]
    issued = [
        audit
        for audit in audits
        if audit['event']['type'] == 'achievement.auto.issue'
        and audit['event']['subject'] == subject
    ]
    assert len(issued) == 1
    assert issued[0]['event']['data']['certificate_id'] == replay.data['grant']['id']
    listed = await call(app, 'achievement.list', {'subject_id': subject})
    assert listed.status == 'ok' and len(listed.data['achievements']) == 1
    assert listed.data['achievements'][0] == replay.data['grant']
