"""Read-only honor diagnostics and the isolated selftest's real protocol client."""
from msg.bootstrap import manifest
from msg.core.codec import canonical, decode, digest
from msg.core.errors import require
from msg.core.models import ResourceRef, Signature
from msg.plugins.achievements import (
    CEREMONY_TTL_SECONDS,
    FINAL_STATEMENT,
    I_AM_NOT_HUMAN,
    MAX_PINS,
    ROUND_TTL_SECONDS,
    STRATEGY,
    STRATEGY_VERSION,
)
from msg.security.crypto import Ed25519Signer, verify


async def inspect_honors(app, tx):
    for table, required in {
        'achievement_ceremonies': {'id', 'subject', 'state', 'body'},
        'achievement_grants': {'id', 'subject', 'achievement_id', 'spec_version', 'body'},
        'achievement_pins': {'subject', 'grant_id', 'position'},
    }.items():
        columns = {row[0] for row in tx.rows(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=?", (table,))}
        require(required <= columns, 'honor_schema_missing', table)
    for name in ('start', 'answer', 'finish', 'pin', 'unpin', 'reorder', 'list'):
        spec = app.registry.operation('achievement.' + name)
        require(spec.effect == ('read' if name == 'list' else 'transaction'),
                'honor_contract_invalid')
        if name in {'finish', 'pin', 'unpin', 'reorder'}:
            require(spec.require_signature, 'honor_contract_invalid')
    sample = manifest()['honor_sample']
    require(sample['policy'] == {
        'achievement_id': I_AM_NOT_HUMAN.id, 'spec_version': I_AM_NOT_HUMAN.version,
        'rounds': 5, 'round_ttl_seconds': ROUND_TTL_SECONDS,
        'ceremony_ttl_seconds': CEREMONY_TTL_SECONDS, 'strategy': STRATEGY,
        'strategy_version': STRATEGY_VERSION, 'privileges': False,
    } and sample['profile']['max_pins'] == MAX_PINS and
        sample['profile']['pinned_grant_ids'] == [] and sample['grants'] == [] and
        sample['issuer']['claim'] == I_AM_NOT_HUMAN.claim and
        sample['issuer']['purpose'] == 'achievement-grant' and
        sample['issuer']['key_source'] == 'service_receipt_signer', 'honor_sample_drift')
    resource = await tx.resource('r_honor_sample')
    revision = await tx.revision(ResourceRef(id=resource.id, revision=resource.revision))
    require(revision.content is not None and
            revision.content.digest == digest(canonical(sample)),
            'honor_sample_drift')
    # Doctor does not load Application storage (which can create directories).
    # Read the installed issuer directly, as the online-CA doctor does.
    issuer = Ed25519Signer.from_bytes((app.settings.service_keys / 'receipt.key').read_bytes())
    probe = canonical({'purpose': 'read-only-honor-doctor'})
    verify(issuer.public_key, probe,
           issuer.sign(probe, purpose='achievement-grant'),
           purpose='achievement-grant')
    return {'read_only': True, 'issuer': issuer.key_id,
            'strategy': STRATEGY, 'spec_version': I_AM_NOT_HUMAN.version,
            'pregrant': False, 'privileges': False}


async def check_honors(app, call, register):
    """Run only on the temporary selftest instance; declarations are test fixtures."""
    key, subject = await register('honor-selftest')
    stranger, other = await register('honor-stranger')
    async with app.metadata.transaction(write=False) as tx:
        security_before = (tx.rows('SELECT body FROM certificates ORDER BY id'),
                           tx.rows('SELECT body FROM credentials ORDER BY id'))
    empty = await call('achievement.list', {'subject_id': subject})
    require(empty.status == 'ok' and not empty.data['achievements'] and
            not empty.data['pinned_grant_ids'], 'selftest_honor_pregranted')
    started = await call('achievement.start', {}, key, subject)
    require(started.status == 'ok', 'selftest_honor_start_failed')
    challenge = started.data
    for number in range(1, 5):
        require(challenge.get('round') == number, 'selftest_honor_round_invalid')
        answer = 'y'
        if number == 4:
            bits = ''.join('1' if char == '\u200c' else '0' for char in challenge['question']
                           if char in {'\u200b', '\u200c'})
            require(len(bits) == 16, 'selftest_honor_payload_invalid')
            answer = ''.join(str(int(bits[i:i + 4], 2)) for i in range(0, 16, 4))
        result = await call('achievement.answer', {
            'challenge_id': challenge['challenge_id'], 'round': number,
            'question_digest': challenge['question_digest'], 'nonce': challenge['nonce'],
            'answer': answer,
        }, key, subject)
        require(result.status == 'ok' and result.data.get('round') == number + 1,
                'selftest_honor_answer_failed')
        challenge = result.data
    finished = await call('achievement.finish', {
        **{field: challenge[field] for field in
           ('challenge_id', 'question_digest', 'nonce', 'ceremony_digest')},
        'statement': FINAL_STATEMENT,
    }, key, subject)
    require(finished.status == 'ok' and 'grant' in finished.data,
            'selftest_honor_finish_failed')
    grant = dict(finished.data['grant'])
    signature = decode(Signature, grant.pop('signature'))
    verify(app.receipt_signer.public_key, canonical(grant), signature,
           purpose='achievement-grant')
    gid = grant['id']
    denied = await call('achievement.pin', {'grant_id': gid}, stranger, other)
    pinned = await call('achievement.pin', {'grant_id': gid}, key, subject)
    reordered = await call('achievement.reorder', {'grant_ids': [gid]}, key, subject)
    public = await call('achievement.list', {'subject_id': subject})
    unpinned = await call('achievement.unpin', {'grant_id': gid}, key, subject)
    final = await call('achievement.list', {'subject_id': subject})
    duplicate = await call('achievement.start', {}, key, subject)
    async with app.metadata.transaction(write=False) as tx:
        security_after = (tx.rows('SELECT body FROM certificates ORDER BY id'),
                          tx.rows('SELECT body FROM credentials ORDER BY id'))
        count = tx.one('SELECT COUNT(*) FROM achievement_grants WHERE subject=?', (subject,))[0]
    return (denied.error is not None and denied.error.code == 'achievement_grant_not_found' and
            all(result.status == 'ok' for result in (pinned, reordered, public, unpinned, final)) and
            list(public.data['pinned_grant_ids']) == [gid] and
            not final.data['pinned_grant_ids'] and len(final.data['achievements']) == 1 and
            count == 1 and duplicate.error is not None and
            duplicate.error.code == 'achievement_already_granted' and
            security_after == security_before)
