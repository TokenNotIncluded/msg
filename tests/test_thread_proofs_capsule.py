"""Fork isolation, revision-bound claims, and participant-only capsules."""

from dataclasses import replace

import pytest
from test_service import call, register

from msg.core.codec import wire


async def make_post(app, key, subject, body='Original context'):
    result = await call(
        app, 'content.post_create', {'parent': '/main', 'body': body}, key=key, subject=subject
    )
    assert result.status == 'ok', wire(result)
    return result.resources[0]


@pytest.mark.asyncio
async def test_fork_is_independent_and_keeps_exact_source(installed):
    app, _ = installed
    key, user, _ = await register(app, 'fork-author')
    source = await make_post(app, key, user)
    result = await call(
        app,
        'discussion.fork',
        {'target': wire(source), 'body': 'Different approach'},
        key=key,
        subject=user,
    )
    assert result.status == 'ok', wire(result)
    branch = result.resources[0]
    reply = await call(
        app,
        'discussion.reply',
        {'target': wire(branch), 'body': 'Branch reply'},
        key=key,
        subject=user,
    )
    assert reply.status == 'ok', wire(reply)
    original_thread = await call(app, 'discussion.thread', {'id': source.id})
    branch_thread = await call(app, 'discussion.thread', {'id': branch.id})
    assert [x['id'] for x in original_thread.data['items']] == [source.id]
    assert branch_thread.data['root'] == branch.id
    assert {x['id'] for x in branch_thread.data['items']} == {branch.id, reply.resources[0].id}
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(branch)
        assert [(r.type, r.target) for r in revision.relations] == [('fork_of', source)]
    forks = await call(app, 'discussion.forks', {'id': source.id})
    assert [x['id'] for x in forks.data['items']] == [branch.id]
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(branch.id)
        await tx.replace(
            replace(resource, mode=0o700, generation=resource.generation + 1), resource.generation
        )
    forks = await call(app, 'discussion.forks', {'id': source.id})
    assert not forks.data['items']


@pytest.mark.asyncio
async def test_claims_digest_pagination_replay_and_legacy_ack(installed):
    app, _ = installed
    key, user, _ = await register(app, 'proof-author')
    source = await make_post(app, key, user)
    view = await call(
        app,
        'discovery.get',
        {'id': source.id, 'revision': source.revision, 'fields': ['id', 'revision', 'digest']},
    )
    digest = view.data['digest']
    bad = await call(
        app,
        'discussion.prove',
        {'target': wire(source), 'digest': 'bad', 'kind': 'USED'},
        key=key,
        subject=user,
    )
    assert bad.error.code == 'proof_digest_mismatch'
    for kind in ('ACK', 'USED', 'VERIFED', 'SOLVED', 'THANKS'):
        args = {
            'target': wire(source),
            'digest': digest,
            'kind': kind,
            'note': 'Reproduced locally',
        }
        for _ in range(2):
            result = await call(app, 'discussion.prove', args, key=key, subject=user)
            assert result.status == 'ok', wire(result)
    legacy = await call(
        app, 'discussion.ack', {'target': wire(source), 'digest': digest}, key=key, subject=user
    )
    assert legacy.status == 'ok'
    page = await call(app, 'discussion.proofs', {'id': source.id, 'limit': 2})
    assert page.data['proofs'] == dict.fromkeys(('ACK', 'USED', 'VERIFIED', 'SOLVED', 'THANKS'), 1)
    items = list(page.data['items'])
    while page.data.get('cursor'):
        page = await call(
            app, 'discussion.proofs', {'id': source.id, 'limit': 2, 'cursor': page.data['cursor']}
        )
        items.extend(page.data['items'])
    assert len(items) == 5
    assert all(
        x['auth'] == 'signature' and x['signed_envelope'] and x['revision'] == source.revision
        for x in items
    )
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(source.id)).generation
    edited = await call(
        app,
        'content.post_edit',
        {'id': source.id, 'expected_revision': source.revision, 'body': 'Updated context'},
        key=key,
        subject=user,
        expected=((source.id, generation),),
    )
    assert edited.status == 'ok', wire(edited)
    current = await call(app, 'discussion.proofs', {'id': source.id})
    assert not current.data['items'] and not any(current.data['proofs'].values())
    history = await call(app, 'discussion.proofs', {'id': source.id, 'revision': source.revision})
    assert len(history.data['items']) == 5
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(source.id)
        await tx.replace(
            replace(resource, mode=0o700, generation=resource.generation + 1), resource.generation
        )
    denied = await call(app, 'discussion.proofs', {'id': source.id})
    assert denied.error.code == 'permission_denied'


@pytest.mark.asyncio
async def test_capsule_roundtrip_private_and_decision(installed):
    app, _ = installed
    key, sender, _ = await register(app, 'capsule-sender')
    other_key, recipient, _ = await register(app, 'capsule-recipient')
    stranger_key, stranger, _ = await register(app, 'capsule-stranger')
    capsule = {
        'goal': 'Continue the branch',
        'progress': 'Implemented fork',
        'verification': 'Tests passed',
        'next_steps': ['Review deployment'],
        'constraints': 'No production writes',
    }
    result = await call(
        app,
        'communication.handoff_create',
        {'to_subject': recipient, 'resource_refs': [], 'capsule': capsule},
        key=key,
        subject=sender,
        contract_version=2,
    )
    assert result.status == 'ok', wire(result)
    hid = result.data['handoff']['id']
    assert 'capsule' not in result.data['handoff']
    got = await call(
        app, 'communication.handoff_get', {'id': hid}, key=other_key, subject=recipient
    )
    assert wire(got.data['handoff']['capsule']) == {'format': 'msg.handoff-capsule/1', **capsule}
    denied = await call(
        app, 'communication.handoff_get', {'id': hid}, key=stranger_key, subject=stranger
    )
    assert denied.error.code == 'handoff_not_found'
    accepted = await call(
        app,
        'communication.handoff_decide',
        {'id': hid, 'decision': 'accept', 'expected_generation': 1},
        key=other_key,
        subject=recipient,
    )
    assert accepted.status == 'ok'
    got = await call(
        app, 'communication.handoff_get', {'id': hid}, key=other_key, subject=recipient
    )
    assert got.data['handoff']['capsule']['goal'] == capsule['goal']
