"""Honor ceremonies use real authentication, transactions and PostgreSQL uniqueness."""

from datetime import timedelta

import pytest
from test_service import NOW, call, register

from msg.core.codec import loads, wire
from msg.plugins.achievements import FINAL_STATEMENT


def answer_for(challenge):
    if challenge['round'] <= 3:
        return 'y'
    bits = ''.join(
        '1' if char == '\u200c' else '0'
        for char in challenge['question']
        if char in {'\u200b', '\u200c'}
    )
    return ''.join(str(int(bits[i : i + 4], 2)) for i in range(0, len(bits), 4))


async def advance(app, key, subject, challenge, *, answer=None):
    return await call(
        app,
        'achievement.answer',
        {
            'challenge_id': challenge['challenge_id'],
            'round': challenge['round'],
            'question_digest': challenge['question_digest'],
            'nonce': challenge['nonce'],
            'answer': answer if answer is not None else answer_for(challenge),
        },
        key=key,
        subject=subject,
    )


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
    finished = await call(
        app,
        'achievement.finish',
        {
            'challenge_id': challenge['challenge_id'],
            'question_digest': challenge['question_digest'],
            'nonce': challenge['nonce'],
            'ceremony_digest': challenge['ceremony_digest'],
            'statement': FINAL_STATEMENT,
        },
        key=key,
        subject=subject,
    )
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
    bad = await call(
        app,
        'achievement.finish',
        {
            'challenge_id': challenge['challenge_id'],
            'question_digest': challenge['question_digest'],
            'nonce': challenge['nonce'],
            'ceremony_digest': 'sha256:' + '0' * 64,
            'statement': FINAL_STATEMENT,
        },
        key=key,
        subject=subject,
    )
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
    expired = await call(
        app,
        'achievement.finish',
        {
            'challenge_id': challenge['challenge_id'],
            'question_digest': challenge['question_digest'],
            'nonce': challenge['nonce'],
            'ceremony_digest': challenge['ceremony_digest'],
            'statement': FINAL_STATEMENT,
        },
        key=key,
        subject=subject,
    )
    assert expired.data['reason'] == 'ceremony_expired'


@pytest.mark.asyncio
async def test_expired_round_can_restart_without_submitting_old_answer(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'achievement-round-restart')
    started = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert started.status == 'ok', wire(started)
    app.executor.clock = lambda: NOW + timedelta(seconds=59)
    live = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert live.status == 'error' and live.error.code == 'achievement_ceremony_active'

    # At the inclusive round deadline no answer can succeed. Restart must not
    # require a doomed answer or waiting for the longer ceremony deadline.
    app.executor.clock = lambda: NOW + timedelta(seconds=60)
    restarted = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert restarted.status == 'ok', wire(restarted)
    assert restarted.data['round'] == 1
    assert restarted.data['challenge_id'] != started.data['challenge_id']
    assert restarted.data['nonce'] != started.data['nonce']
    stale = await advance(app, key, subject, started.data)
    assert stale.status == 'error' and stale.error.code == 'achievement_ceremony_not_found'
    advanced = await advance(app, key, subject, restarted.data)
    assert advanced.status == 'ok' and advanced.data['round'] == 2
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('retired', [False, True])
async def test_custodial_ceremony_signs_final_request_and_labels_source(installed, retired):
    import os

    from msg.core.codec import b64, canonical, decode, unb64
    from msg.core.models import Signature
    from msg.security.crypto import verify

    app, _ = installed
    created = await call(
        app,
        'identity.custodial_create',
        {
            'handle': 'achievement-custodial',
            'nonce': b64(os.urandom(32)),
            'recovery_secret': b64(os.urandom(32)),
        },
        contract_version=2,
    )
    assert created.status == 'ok', wire(created)
    subject = created.data['subject_id']
    token = (created.data['credential_id'], unb64(created.data['token']))
    challenge = await call(app, 'achievement.start', {}, subject=subject, token=token)
    assert challenge.status == 'ok', wire(challenge)
    for _ in range(4):
        data = challenge.data
        challenge = await call(
            app,
            'achievement.answer',
            {
                'challenge_id': data['challenge_id'],
                'round': data['round'],
                'question_digest': data['question_digest'],
                'nonce': data['nonce'],
                'answer': answer_for(data),
            },
            subject=subject,
            token=token,
        )
        assert challenge.status == 'ok', wire(challenge)
    data = challenge.data
    args = {
        name: data[name] for name in ('challenge_id', 'question_digest', 'nonce', 'ceremony_digest')
    }
    args['statement'] = FINAL_STATEMENT
    if retired:
        async with app.metadata.transaction(write=True) as tx:
            tx.execute(
                "UPDATE custodial_vault SET status='decrypt_only', signing_nonce=NULL, signing_ciphertext=NULL WHERE subject=?",
                (subject,),
                write=True,
            )
    finished = await call(app, 'achievement.finish', args, subject=subject, token=token)
    if retired:
        assert finished.status == 'error' and finished.error.code == 'custodial_vault_unavailable'
        async with app.metadata.transaction(write=False) as tx:
            assert (
                tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0]
                == 0
            )
        return
    assert finished.status == 'ok', wire(finished)
    assert finished.data['signature_source'] == 'custodial'
    grant = finished.data['grant']
    assert grant['auth_method'] == 'token'
    assert grant['metadata'] == {'protocol_passed': True, 'signature_source': 'custodial'}
    async with app.metadata.transaction(write=False) as tx:
        state = loads(
            tx.one('SELECT body FROM achievement_ceremonies WHERE id=?', (data['challenge_id'],))[0]
        )
        credential = await tx.credential(state['final_signature']['key_id'])
        verify(
            credential.verifier,
            canonical(state['final_confirmation']),
            decode(Signature, state['final_signature']),
            purpose='achievement-confirmation',
        )
        assert state['final_confirmation']['ceremony_digest'] == data['ceremony_digest']
        audit = [loads(row[0]) for row in tx.rows('SELECT body FROM audit')]
    issued = [row for row in audit if row['event']['type'] == 'achievement.auto.issue']
    assert len(issued) == 1
    assert issued[0]['event']['data']['signature_source'] == 'custodial'
    assert issued[0]['event']['data']['auth_method'] == 'token'
    repeat = await call(app, 'achievement.start', {}, subject=subject, token=token)
    assert repeat.status == 'error' and repeat.error.code == 'achievement_already_granted'
    private = await call(app, 'discovery.get', {'id': '/private'}, subject=subject, token=token)
    assert private.status == 'error'


@pytest.mark.asyncio
async def test_non_custodial_token_cannot_finish_ceremony(installed):
    from test_service import temporary_v3_args

    from msg.core.codec import unb64

    app, _ = installed
    args, rid, _, _ = temporary_v3_args()
    created = await call(app, 'identity.temporary', args, rid=rid, contract_version=3)
    assert created.status == 'ok', wire(created)
    subject = created.data['subject_id']
    token = (created.data['credential_id'], unb64(created.data['token']))
    started = await call(app, 'achievement.start', {}, subject=subject, token=token)
    assert started.status == 'ok', wire(started)
    denied = await call(
        app,
        'achievement.finish',
        {
            'challenge_id': started.data['challenge_id'],
            'question_digest': started.data['question_digest'],
            'nonce': started.data['nonce'],
            'ceremony_digest': 'sha256:' + '0' * 64,
            'statement': FINAL_STATEMENT,
        },
        subject=subject,
        token=token,
    )
    assert denied.status == 'error' and denied.error.code == 'signature_required'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0] == 0
