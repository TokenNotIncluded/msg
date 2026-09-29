"""Current metadata can be staged only while all old/new derived authority is isolated."""

from copy import deepcopy

import pytest
from test_recovery_policy_v2 import packet_for, replay
from test_service import NOW, call, register

from msg.core.codec import wire
from msg.core.errors import Failure
from msg.core.models import EffectJob, Principal
from msg.security.quarantine import SETTING, active
from msg.workers.effects import EffectWorker


@pytest.fixture
async def authority_restore(installed):
    app, _ = installed
    old = await register(app, 'old-authority')
    new = await register(app, 'new-authority')
    created = await call(
        app,
        'content.topic_create',
        {'parent': old[1], 'name': 'staged'},
        key=old[0],
        subject=old[1],
    )
    assert created.status == 'ok'
    target = created.resources[0].id
    assert (
        await call(app, 'discovery.get', {'id': target}, key=old[0], subject=old[1])
    ).status == 'ok'
    job = EffectJob(
        id='authority-mail',
        event_id='old-event',
        kind='mail',
        dedupe_key='authority-mail',
        principal=Principal(
            actor=old[1],
            subject=old[1],
            credential_id=None,
            method='local',
            certificates=(),
            ceiling=(),
        ),
        operation='mail.send',
        arguments={},
        state='pending',
        attempts=0,
        next_attempt_at=NOW,
        lease_until=None,
    )
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(target)
        await tx.enqueue(job)
        tx.set_setting(
            SETTING,
            {
                'format': 'msg-recovery-quarantine-v1',
                'source_backup_sha256': 'a' * 64,
                'outbound_enabled': False,
                'authority': 'health_only',
            },
        )
    body, signed, pin = packet_for(resource)
    previous = {key: getattr(resource, key) for key in ('owner', 'group', 'parent')}
    body['entries'][0].update(
        kind='resource.authority.reconcile',
        value={'previous': previous, 'current': dict(previous, owner=new[1], parent=new[1])},
    )
    body['coverage']['domains'] = ['resource_authority']
    return app, old, new, resource, job, body, signed, pin


async def assert_isolated(app, users, resource, job):
    for key, subject, cert in users:
        for operation, args in [
            ('discovery.get', {'id': resource.id}),
            ('content.chmod', {'id': resource.id, 'mode': '0777'}),
        ]:
            result = await call(app, operation, args, key=key, subject=subject, certs=(cert,))
            assert result.error.code == (
                'recovery_quarantined' if operation == 'discovery.get' else 'writes_paused'
            )
    worker = EffectWorker(app)
    assert await worker._claim() == (None, False)
    assert await worker.run_once() is False
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.job(job.id) == job
        assert active(tx)


async def test_stage_new_owner_parent_without_exposing_old_or_new_scope(authority_restore):
    app, old, new, resource, job, body, signed, pin = authority_restore
    await assert_isolated(app, (old, new), resource, job)
    async with app.metadata.transaction(write=False) as tx:
        revisions = tx.rows('SELECT body FROM revisions ORDER BY id')
        certificates = tx.rows('SELECT body,revoked FROM certificates ORDER BY id')
    result = await replay(app, body, signed, pin)
    assert result['changed'] == 1 and result['promotion'] == 'blocked'
    assert result['authority_rebuild_required'] == [resource.id]
    assert 'derived_authority_inventory_incomplete' in result['promotion_blocked_reasons']
    assert (await replay(app, body, signed, pin))['changed'] == 0
    await assert_isolated(app, (old, new), resource, job)
    async with app.metadata.transaction(write=False) as tx:
        updated = await tx.resource(resource.id)
        assert (updated.owner, updated.parent, updated.mode) == (new[1], new[1], resource.mode)
        assert tx.rows('SELECT body FROM revisions ORDER BY id') == revisions
        assert tx.rows('SELECT body,revoked FROM certificates ORDER BY id') == certificates


@pytest.mark.parametrize('invalid', ['state', 'cycle', 'missing_parent', 'missing_owner'])
async def test_bad_reconciliation_rolls_back_earlier_certificate_revocation(
    authority_restore, invalid
):
    app, old, new, resource, job, body, signed, pin = authority_restore
    fact = body['entries'][0]
    if invalid == 'state':
        fact['value']['previous']['parent'] = 'unrelated'
    elif invalid == 'cycle':
        fact['value']['current']['parent'] = resource.id
    elif invalid == 'missing_parent':
        fact['value']['current']['parent'] = 'missing'
    else:
        fact['value']['current']['owner'] = 'missing'
    fact['sequence'] = 2
    body['entries'] = [
        {
            'sequence': 1,
            'kind': 'certificate.current',
            'subject': old[1],
            'target': old[2],
            'at': wire(NOW),
            'value': {'body_digest': None},
        },
        fact,
    ]
    body['sequence'] = 2
    body['coverage']['domains'] = ['certificates', 'resource_authority']
    with pytest.raises(Failure):
        await replay(app, body, signed, pin)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.resource(resource.id) == resource
        assert not await tx.certificate_revoked(old[2])
        assert tx.setting('recovery_replay') is None and active(tx)
    await assert_isolated(app, (old, new), resource, job)


async def test_contiguous_owner_chain_converges_once_and_rejects_gap(authority_restore):
    app, old, new, resource, job, body, signed, pin = authority_restore
    second = deepcopy(body['entries'][0])
    second['sequence'] = 2
    second['subject'] = new[1]
    second['value'] = {
        'previous': second['value']['current'],
        'current': dict(second['value']['current'], parent=old[1]),
    }
    body['entries'].append(second)
    body['sequence'] = 2
    assert (await replay(app, body, signed, pin))['changed'] == 1
    assert (await replay(app, body, signed, pin))['changed'] == 0
    await assert_isolated(app, (old, new), resource, job)
    async with app.metadata.transaction(write=False) as tx:
        actual = await tx.resource(resource.id)
        assert actual.owner == new[1] and actual.parent == old[1]
    second['value']['previous']['group'] = 'unrelated'
    with pytest.raises(Failure, match='recovery_checkpoint_invalid'):
        await replay(app, body, signed, pin)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.resource(resource.id) == actual


async def test_acl_then_transfer_replays_twice(authority_restore):
    app, old, new, resource, job, body, signed, pin = authority_restore
    transfer = body['entries'][0]
    transfer['sequence'] = 2
    acl = dict(
        transfer,
        sequence=1,
        kind='resource.acl.restrict',
        value={'owner': resource.owner, 'group': resource.group, 'mode': 0o700},
    )
    body.update(entries=[acl, transfer], sequence=2)
    body['coverage']['domains'] = ['resource_acl', 'resource_authority']
    await replay(app, body, signed, pin)
    assert (await replay(app, body, signed, pin))['changed'] == 0
    await assert_isolated(app, (old, new), resource, job)


@pytest.mark.parametrize('bad_binding', [None, 'owner', 'group', 'past_owner'])
async def test_intermediate_acl_binds_to_signed_authority_chain(authority_restore, bad_binding):
    app, old, new, resource, job, body, signed, pin = authority_restore
    first = body['entries'][0]
    middle = dict(
        first,
        sequence=2,
        subject=new[1],
        kind='resource.acl.restrict',
        value={'owner': new[1], 'group': resource.group, 'mode': 0o400},
    )
    final = dict(
        first,
        sequence=3,
        subject=new[1],
        value={
            'previous': deepcopy(first['value']['current']),
            'current': dict(first['value']['previous']),
        },
    )
    body.update(entries=[first, middle, final], sequence=3)
    body['coverage']['domains'] = ['resource_acl', 'resource_authority']
    if bad_binding:
        if bad_binding == 'past_owner':
            middle['value']['owner'] = old[1]
            middle['subject'] = old[1]
        else:
            middle['value'][bad_binding] = 'unrelated'
        with pytest.raises(Failure, match='recovery_fact_subject_mismatch'):
            await replay(app, body, signed, pin)
        async with app.metadata.transaction(write=False) as tx:
            assert await tx.resource(resource.id) == resource
            assert tx.setting('recovery_replay') is None
    else:
        await replay(app, body, signed, pin)
        assert (await replay(app, body, signed, pin))['changed'] == 0
        async with app.metadata.transaction(write=False) as tx:
            actual = await tx.resource(resource.id)
            assert actual.owner == old[1] and actual.mode == (
                (resource.mode & 0o7000) | (resource.mode & 0o400)
            )
    await assert_isolated(app, (old, new), resource, job)
