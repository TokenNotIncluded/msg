"""Pinned typed ceilings shrink old authorization without claiming complete recovery."""
from copy import deepcopy
from datetime import timedelta

import pytest

from msg.admin.recovery_replay import TrustedCheckpointPin, _replay, verify_checkpoint, POLICY_FORMAT
from msg.core.codec import canonical, digest, wire
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer
from msg.security.quarantine import SETTING, active
from test_service import NOW


@pytest.fixture
async def policy_target(installed):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource('u_root')
        tx.set_setting(SETTING, {'format': 'msg-recovery-quarantine-v1',
            'source_backup_sha256': 'a' * 64, 'outbound_enabled': False, 'authority': 'health_only'})
    return app, resource


def packet_for(resource, *, mode=0, complete=False):
    signer = Ed25519Signer.generate()
    body = {'format': POLICY_FORMAT, 'service': 'http://testserver',
        'source_backup_sha256': 'a' * 64, 'sequence': 1,
        'coverage': {'complete': complete, 'domains': ['resource_acl']},
        'entries': [{'sequence': 1, 'kind': 'resource.acl.restrict', 'subject': resource.owner,
            'target': resource.id, 'at': wire(NOW),
            'value': {'owner': resource.owner, 'group': resource.group, 'mode': mode}}]}
    def signed(body, purpose='recovery-checkpoint-v2'):
        return {'checkpoint': body, 'signature': wire(signer.sign(canonical(body), purpose=purpose))}
    def pin(body):
        return TrustedCheckpointPin(service=body['service'], public_key=signer.public_key,
            digest=digest(body), sequence=body['sequence'])
    return body, signed, pin


async def replay(app, body, signed, pin):
    return await _replay(app.metadata, signed(body), pin=pin(body), operator='isolated-test')


async def test_v2_acl_is_monotonic_idempotent_and_preserves_history(policy_target):
    app, resource = policy_target
    body, signed, pin = packet_for(resource)
    async with app.metadata.transaction(write=False) as tx:
        history = tx.rows('SELECT body FROM revisions ORDER BY id')
    result = await replay(app, body, signed, pin)
    assert result['changed'] == 1 and result['promotion'] == 'blocked'
    assert result['coverage'] == {'complete': False, 'domains': ['resource_acl']}
    assert (await replay(app, body, signed, pin))['changed'] == 0
    async with app.metadata.transaction(write=False) as tx:
        updated = await tx.resource(resource.id)
        assert updated.mode == 0 and updated.generation == resource.generation + 1
        assert updated.revision == resource.revision and active(tx)
        assert tx.rows('SELECT body FROM revisions ORDER BY id') == history
    # A newly pinned broader ceiling cannot restore rights already removed.
    broader, signed2, pin2 = packet_for(resource, mode=0o777)
    broader['entries'] = body['entries'] + [dict(broader['entries'][0], sequence=2)]
    broader['sequence'] = 2
    assert (await replay(app, broader, signed2, pin2))['changed'] == 0


@pytest.mark.parametrize('change', ['complete', 'coverage', 'owner', 'bool', 'extra', 'purpose', 'v1'])
async def test_v2_rejects_unsupported_authority_and_contracts(policy_target, change):
    app, resource = policy_target
    body, signed, pin = packet_for(resource)
    if change == 'complete':
        body['coverage']['complete'] = True
    elif change == 'coverage':
        body['coverage']['domains'] = []
    elif change == 'owner':
        body['entries'][0]['value']['owner'] = 'u_other'
    elif change == 'bool':
        body['entries'][0]['value']['mode'] = True
    elif change == 'extra':
        body['entries'][0]['value']['grant'] = 'root'
    elif change == 'v1':
        body['format'] = 'msg-revocation-checkpoint-v1'
        body.pop('coverage')
    with pytest.raises(Failure):
        if change == 'purpose':
            verify_checkpoint(signed(body, 'recovery-checkpoint-v1'), pin=pin(body))
        else:
            await replay(app, body, signed, pin)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.resource(resource.id) == resource
        assert tx.setting('recovery_replay') is None and active(tx)


async def test_v2_policy_ban_expiry_and_rollback(policy_target):
    from msg.core.models import Resource
    app, owner = policy_target
    topic = Resource(id='t_recovery_policy', type='topic', type_version=1, name='policy',
        parent=owner.id, owner=owner.id, group='g_public', mode=0o777, generation=1,
        revision=None, state='active', created_at=NOW, created_by=owner.id,
        modified_at=NOW, modified_by=owner.id)
    async with app.metadata.transaction(write=True) as tx:
        await tx.insert(topic)
        tx.execute('INSERT INTO topic_memberships VALUES (?,?,?,?,?,NULL)',
                   (topic.id, owner.id, 'admin', 'active', wire(NOW)), write=True)
    body, signed, pin = packet_for(topic)
    fact = {'sequence': 2, 'kind': 'topic.policy.restrict', 'subject': owner.id,
            'target': topic.id, 'at': wire(NOW), 'value': {'membership_policy': 'invite'}}
    ban = dict(fact, sequence=3, kind='topic_ban.set',
               value={'expires_at': wire(NOW + timedelta(days=1))})
    body.update(sequence=3, entries=body['entries'] + [fact, ban],
                coverage={'complete': False, 'domains': ['resource_acl', 'topic_ban', 'topic_policy']})
    bad = deepcopy(body)
    bad['entries'][2]['target'] = 'missing'
    with pytest.raises(Failure):
        await replay(app, bad, signed, pin)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(topic.id)).mode == 0o777
        assert tx.one('SELECT membership_policy FROM topic_settings WHERE topic=?', (topic.id,)) is None
    assert (await replay(app, body, signed, pin))['changed'] == 3
    assert (await replay(app, body, signed, pin))['changed'] == 0
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT membership_policy FROM topic_settings WHERE topic=?', (topic.id,)) == ('invite',)
        assert tx.one('SELECT expires_at FROM topic_bans WHERE topic=?', (topic.id,)) == (ban['value']['expires_at'],)
        assert tx.one('SELECT role,status FROM topic_memberships WHERE topic=?', (topic.id,)) == ('member', 'removed')
        assert active(tx)

    # Approval and invite cannot be ordered; their safe intersection stays closed.
    body['entries'].append(dict(fact, sequence=4, value={'membership_policy': 'approval'}))
    body['sequence'] = 4
    assert (await replay(app, body, signed, pin))['changed'] == 1
    assert (await replay(app, body, signed, pin))['changed'] == 0
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT membership_policy FROM topic_settings WHERE topic=?', (topic.id,)) == ('closed',)


def test_packaged_policy_schema_matches_typed_contract():
    import json
    from importlib.resources import files
    import jsonschema
    from types import SimpleNamespace
    resource = SimpleNamespace(id='t_test', owner='u_owner', group='g_public')
    body, signed, pin = packet_for(resource)
    schema = json.loads(files('msg.data').joinpath('recovery-policy-checkpoint.schema.json').read_text())
    jsonschema.validate(signed(body), schema)
    assert verify_checkpoint(signed(body), pin=pin(body)) == body
    body['coverage']['complete'] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(signed(body), schema)


async def test_v1_prefix_can_continue_in_v2_but_not_downgrade(policy_target):
    app, resource = policy_target
    body, signed, pin = packet_for(resource)
    old = dict(body, format='msg-revocation-checkpoint-v1', sequence=0, entries=[])
    old.pop('coverage')
    await _replay(app.metadata, signed(old, 'recovery-checkpoint-v1'), pin=pin(old), operator='isolated-test')
    await replay(app, body, signed, pin)
    with pytest.raises(Failure, match='recovery_checkpoint_regression'):
        await _replay(app.metadata, signed(old, 'recovery-checkpoint-v1'), pin=pin(old), operator='isolated-test')
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('recovery_replay')['format'] == POLICY_FORMAT and active(tx)


@pytest.mark.parametrize('existing_days', [None, 1, 3])
async def test_v2_ban_never_shortens_an_active_restriction(policy_target, existing_days):
    from dataclasses import replace
    app, owner = policy_target
    topic = replace(owner, id='t_existing_ban', type='topic', name='existing-ban',
                    parent=owner.id, revision=None, generation=1)
    previous = wire(NOW + timedelta(days=existing_days)) if existing_days else None
    desired = wire(NOW + timedelta(days=2))
    async with app.metadata.transaction(write=True) as tx:
        await tx.insert(topic)
        tx.execute('INSERT INTO topic_bans VALUES (?,?,?,?,?,?,?)',
                   (topic.id, owner.id, owner.id, wire(NOW), previous, 'prior', 'active'), write=True)
    body, signed, pin = packet_for(topic)
    body['entries'][0].update(kind='topic_ban.set', value={'expires_at': desired})
    body['coverage']['domains'] = ['topic_ban']
    await replay(app, body, signed, pin)
    assert (await replay(app, body, signed, pin))['changed'] == 0
    async with app.metadata.transaction(write=False) as tx:
        expected = None if existing_days is None else max(previous, desired)
        assert tx.one('SELECT expires_at FROM topic_bans WHERE topic=?', (topic.id,)) == (expected,)
        assert active(tx)


@pytest.mark.parametrize('special', [0o4000, 0o2000, 0o1000, 0o7000])
async def test_acl_restriction_preserves_existing_special_security_bits(policy_target, special):
    from dataclasses import replace
    app, resource = policy_target
    async with app.metadata.transaction(write=True) as tx:
        resource = replace(resource, mode=special | 0o777, generation=resource.generation + 1)
        await tx.replace(resource, resource.generation - 1)
    body, signed, pin = packet_for(resource, mode=0o400)
    await replay(app, body, signed, pin)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(resource.id)).mode == special | 0o400
        assert active(tx)
