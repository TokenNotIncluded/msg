"""A proposal binds immutable content; only explicit current write authority accepts it."""

import pytest
from test_service import call, register

from msg.core.codec import wire


async def fixture(app):
    key, subject, _ = await register(app, 'proposal-owner')
    target = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'Old text'},
        key=key,
        subject=subject,
    )
    source = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'Proposed text'},
        key=key,
        subject=subject,
    )
    args = {
        'target': target.resources[0].id,
        'base_revision': target.resources[0].revision,
        'content_ref': wire(source.resources[0]),
        'message': 'Please apply',
    }
    proposal = await call(app, 'communication.proposal_create', args, key=key, subject=subject)
    assert proposal.status == 'ok', proposal.error
    return key, subject, target, source, proposal


def acceptance(target, proposal):
    return {
        'id': proposal.data['id'],
        'proposal_revision': proposal.resources[0].revision,
        'base_revision': target.resources[0].revision,
    }


def generations(target, proposal):
    return (
        (target.resources[0].id, target.data['generation']),
        (proposal.data['id'], proposal.data['generation']),
    )


@pytest.mark.asyncio
async def test_proposal_accept_uses_pinned_content_and_replays_once(installed):
    app, _ = installed
    key, subject, target, source, proposal = await fixture(app)
    changed_source = await call(
        app,
        'content.post_edit',
        {
            'id': source.resources[0].id,
            'expected_revision': source.resources[0].revision,
            'body': 'Later unapproved text',
        },
        key=key,
        subject=subject,
        expected=((source.resources[0].id, source.data['generation']),),
    )
    assert changed_source.status == 'ok', changed_source.error
    accepted = await call(
        app,
        'communication.proposal_accept',
        acceptance(target, proposal),
        key=key,
        subject=subject,
        expected=generations(target, proposal),
        rid='accept-proposal-once',
    )
    assert accepted.status == 'ok', accepted.error
    result = await call(
        app, 'discovery.get', {'id': target.resources[0].id}, key=key, subject=subject
    )
    assert result.data['content'] == 'Proposed text'
    again = await call(
        app,
        'communication.proposal_accept',
        acceptance(target, proposal),
        key=key,
        subject=subject,
        expected=generations(target, proposal),
        rid='accept-proposal-once',
    )
    assert again.replayed and again.resources == accepted.resources
    record = await call(
        app, 'communication.proposal_get', {'id': proposal.data['id']}, key=key, subject=subject
    )
    assert record.data['proposal']['status'] == 'accepted'
    assert record.data['proposal']['target_ref']['id'] == target.resources[0].id


@pytest.mark.asyncio
async def test_proposal_conflict_keeps_open_and_cannot_edit_without_current_rights(installed):
    from datetime import timedelta

    from test_service import NOW

    app, _ = installed
    key, subject, target, source, proposal = await fixture(app)
    other_key, other, _ = await register(app, 'proposal-reader')
    shared = await call(
        app,
        'sharing.grant',
        {
            'resource': proposal.data['id'],
            'grantee': other,
            'expires_at': wire(NOW + timedelta(days=1)),
        },
        key=key,
        subject=subject,
    )
    assert shared.status == 'ok', shared.error
    denied = await call(
        app,
        'communication.proposal_accept',
        acceptance(target, proposal),
        key=other_key,
        subject=other,
        expected=generations(target, proposal),
    )
    assert denied.status == 'error'
    edit = await call(
        app,
        'content.post_edit',
        {
            'id': target.resources[0].id,
            'expected_revision': target.resources[0].revision,
            'body': 'Concurrent change',
        },
        key=key,
        subject=subject,
        expected=((target.resources[0].id, target.data['generation']),),
    )
    assert edit.status == 'ok', edit.error
    conflict = await call(
        app,
        'communication.proposal_accept',
        acceptance(target, proposal),
        key=key,
        subject=subject,
        expected=(
            (target.resources[0].id, edit.data['generation']),
            (proposal.data['id'], proposal.data['generation']),
        ),
    )
    assert conflict.error.code == 'revision_conflict'
    got = await call(
        app, 'communication.proposal_get', {'id': proposal.data['id']}, key=key, subject=subject
    )
    assert got.data['proposal']['status'] == 'open'
    wrong = await call(
        app,
        'communication.proposal_withdraw',
        {'id': proposal.data['id'], 'proposal_revision': source.resources[0].revision},
        key=key,
        subject=subject,
        expected=((proposal.data['id'], proposal.data['generation']),),
    )
    assert wrong.error.code == 'revision_conflict'
    withdrawn = await call(
        app,
        'communication.proposal_withdraw',
        {'id': proposal.data['id'], 'proposal_revision': proposal.resources[0].revision},
        key=key,
        subject=subject,
        expected=((proposal.data['id'], proposal.data['generation']),),
    )
    assert withdrawn.data['status'] == 'withdrawn'


@pytest.mark.asyncio
async def test_proposal_rechecks_content_visibility_and_rejects_other_target_kinds(installed):
    app, _ = installed
    key, subject, target, _, _ = await fixture(app)
    source_key, source_subject, _ = await register(app, 'proposal-source-owner')
    source = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'Shared draft'},
        key=source_key,
        subject=source_subject,
    )
    args = {
        'target': target.resources[0].id,
        'base_revision': target.resources[0].revision,
        'content_ref': wire(source.resources[0]),
        'message': 'Use this draft',
    }
    unsupported = await call(
        app, 'communication.proposal_create', {**args, 'target': '/main'}, key=key, subject=subject
    )
    assert unsupported.error.code == 'proposal_target_kind_unsupported'
    proposal = await call(app, 'communication.proposal_create', args, key=key, subject=subject)
    assert proposal.status == 'ok', proposal.error
    hidden = await call(
        app,
        'content.chmod',
        {'id': source.resources[0].id, 'mode': '0600'},
        key=source_key,
        subject=source_subject,
        expected=((source.resources[0].id, source.data['generation']),),
    )
    assert hidden.status == 'ok', hidden.error
    failed = await call(
        app,
        'communication.proposal_accept',
        acceptance(target, proposal),
        key=key,
        subject=subject,
        expected=generations(target, proposal),
    )
    assert failed.status == 'error'
    got = await call(
        app, 'communication.proposal_get', {'id': proposal.data['id']}, key=key, subject=subject
    )
    assert got.data['proposal']['status'] == 'open'
    assert 'content_ref' not in got.data['proposal']
    generic = await call(
        app, 'discovery.get', {'id': proposal.data['id']}, key=key, subject=subject
    )
    assert source.resources[0].id not in str(generic.data)
    original = await call(
        app, 'discovery.get', {'id': target.resources[0].id}, key=key, subject=subject
    )
    assert original.data['content'] == 'Old text'
