"""Honor ceremonies use real authentication, transactions and PostgreSQL uniqueness."""
from datetime import timedelta

import pytest

from msg.core.codec import loads, wire
from msg.plugins.achievements import FINAL_STATEMENT
from test_service import NOW, call, register


def answer_for(challenge):
    if challenge['round'] <= 3:
        return 'y'
    bits = ''.join('1' if char == '\u200c' else '0' for char in challenge['question']
                   if char in {'\u200b', '\u200c'})
    return ''.join(str(int(bits[i:i + 4], 2)) for i in range(0, len(bits), 4))


async def advance(app, key, subject, challenge, *, answer=None):
    return await call(app, 'achievement.answer', {
        'challenge_id': challenge['challenge_id'], 'round': challenge['round'],
        'question_digest': challenge['question_digest'], 'nonce': challenge['nonce'],
        'answer': answer if answer is not None else answer_for(challenge),
    }, key=key, subject=subject)


@pytest.mark.asyncio
async def test_full_ceremony_issues_one_honor_fact_without_access_power(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'achievement-owner')
    empty = await call(app, 'achievement.list', {'subject_id': subject})
    assert empty.status == 'ok' and empty.data['achievements'] == (), (empty.error, wire(empty))
    started = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert started.status == 'ok', wire(started)
    challenge = started.data
    for expected_round in range(1, 5):
        assert challenge['round'] == expected_round
        step = await advance(app, key, subject, challenge)
        assert step.status == 'ok', wire(step)
        challenge = step.data
    assert challenge['round'] == 5
    finished = await call(app, 'achievement.finish', {
        'challenge_id': challenge['challenge_id'], 'question_digest': challenge['question_digest'],
        'nonce': challenge['nonce'], 'ceremony_digest': challenge['ceremony_digest'],
        'statement': FINAL_STATEMENT,
    }, key=key, subject=subject)
    assert finished.status == 'ok', wire(finished)
    grant = finished.data['grant']
    assert grant['achievement_id'] == 'i-am-not-human'
    assert grant['metadata'] == {'protocol_passed': True}
    assert 'verified_non_human' not in str(grant)
    assert finished.data['signature_source'] == 'self-custody'
    listed = await call(app, 'achievement.list', {'subject_id': subject})
    assert len(listed.data['achievements']) == 1 and listed.data['achievements'][0] == grant
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0] == 1
        audit = [loads(row[0]) for row in tx.rows('SELECT body FROM audit')]
    issued = [row for row in audit if row['event']['type'] == 'achievement.auto.issue']
    assert len(issued) == 1 and issued[0]['event']['data']['certificate_id'] == grant['id']
    private = await call(app, 'discovery.get', {'id': '/private'}, key=key, subject=subject)
    assert private.status == 'error'


@pytest.mark.asyncio
async def test_failed_round_cannot_continue_or_issue(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'achievement-failure')
    started = await call(app, 'achievement.start', {}, key=key, subject=subject)
    challenge = started.data
    failed = await advance(app, key, subject, challenge, answer='n')
    assert failed.status == 'ok' and failed.data['status'] == 'failed'
    retry = await advance(app, key, subject, challenge)
    assert retry.status == 'error' and retry.error.code == 'ceremony_inactive'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0] == 0


@pytest.mark.asyncio
async def test_wrong_nonce_and_round_ttl_fail_closed(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'achievement-nonce')
    started = await call(app, 'achievement.start', {}, key=key, subject=subject)
    nonce = started.data['nonce']
    wrong = dict(started.data, nonce=('A' if nonce[0] != 'A' else 'B') + nonce[1:])
    failed = await advance(app, key, subject, wrong)
    assert failed.data['reason'] == 'nonce_mismatch'
    started = await call(app, 'achievement.start', {}, key=key, subject=subject)
    app.executor.clock = lambda: NOW + timedelta(seconds=61)
    expired = await advance(app, key, subject, started.data)
    assert expired.data['reason'] == 'round_expired'


@pytest.mark.asyncio
async def test_start_keeps_only_one_ceremony_per_subject(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'achievement-bounded')
    started = await call(app, 'achievement.start', {}, key=key, subject=subject)
    duplicate = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert duplicate.status == 'error' and duplicate.error.code == 'achievement_ceremony_active'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_ceremonies WHERE subject=?', (subject,))[0] == 1

    app.executor.clock = lambda: NOW + timedelta(seconds=301)
    replacement = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert replacement.status == 'ok', wire(replacement)
    assert replacement.data['challenge_id'] != started.data['challenge_id']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_ceremonies WHERE subject=?', (subject,))[0] == 1


@pytest.mark.asyncio
async def test_start_obeys_capacity_pause(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'achievement-paused')
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('runtime_config', {'accept_writes': False, 'cleanup_enabled': True})
    blocked = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert blocked.status == 'error' and blocked.error.code == 'writes_paused'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_ceremonies WHERE subject=?', (subject,))[0] == 0


@pytest.mark.asyncio
async def test_machine_payload_and_final_digest_are_required(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'achievement-payload')
    started = await call(app, 'achievement.start', {}, key=key, subject=subject)
    challenge = started.data
    for _ in range(3):
        challenge = (await advance(app, key, subject, challenge)).data
    assert challenge['round'] == 4
    lost_payload = await advance(app, key, subject, challenge, answer='0000')
    assert lost_payload.data['status'] == 'failed'
    challenge = (await call(app, 'achievement.start', {}, key=key, subject=subject)).data
    for _ in range(4):
        challenge = (await advance(app, key, subject, challenge)).data
    bad = await call(app, 'achievement.finish', {
        'challenge_id': challenge['challenge_id'], 'question_digest': challenge['question_digest'],
        'nonce': challenge['nonce'], 'ceremony_digest': 'sha256:' + '0' * 64,
        'statement': FINAL_STATEMENT,
    }, key=key, subject=subject)
    assert bad.data['status'] == 'failed'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0] == 0


@pytest.mark.asyncio
async def test_total_ttl_and_replayed_nonce(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'achievement-replay')
    challenge = (await call(app, 'achievement.start', {}, key=key, subject=subject)).data
    first = await advance(app, key, subject, challenge)
    assert first.status == 'ok'
    replay = await advance(app, key, subject, challenge)
    assert replay.data['status'] == 'failed'
    challenge = (await call(app, 'achievement.start', {}, key=key, subject=subject)).data
    for index in range(1, 5):
        app.executor.clock = lambda index=index: NOW + timedelta(seconds=index * 59)
        challenge = (await advance(app, key, subject, challenge)).data
    app.executor.clock = lambda: NOW + timedelta(seconds=301)
    expired = await call(app, 'achievement.finish', {
        'challenge_id': challenge['challenge_id'], 'question_digest': challenge['question_digest'],
        'nonce': challenge['nonce'], 'ceremony_digest': challenge['ceremony_digest'],
        'statement': FINAL_STATEMENT,
    }, key=key, subject=subject)
    assert expired.data['reason'] == 'ceremony_expired'
