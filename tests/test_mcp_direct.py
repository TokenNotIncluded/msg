"""Token DM versions preserve consent and current authority on real PostgreSQL."""

import os
from dataclasses import replace

import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, unb64, wire
from msg.core.models import Scope

DM_OPERATIONS = {
    'communication.dm_request@3',
    'communication.dm_send@2',
    'communication.dm_accept@2',
    'communication.dm_reject@2',
    'communication.conversation_get@1',
}


async def scoped_token(app, key, subject, operations, *, scope=None):
    grants = tuple(
        replace(grant, operations=grant.operations & operations, scope=scope or grant.scope)
        for grant in app.primary_ceiling()
        if grant.capability in app.base_capability_names and grant.operations & operations
    )
    issued = await call(
        app,
        'identity.token_create',
        {
            'nonce': b64(os.urandom(32)),
            'recovery_secret': b64(os.urandom(32)),
            'ceiling': wire(grants),
            'ttl': 120,
        },
        key=key,
        subject=subject,
        contract_version=3,
    )
    assert issued.status == 'ok', wire(issued)
    return issued.data['credential_id'], unb64(issued.data['token'])


async def dm_call(app, operation, arguments, subject, token, *, version, rid=None):
    return await call(
        app, operation, arguments, subject=subject, token=token, contract_version=version, rid=rid
    )


def assert_denied(result, *codes):
    assert result.status == 'error', wire(result)
    if codes:
        assert result.error.code in codes, wire(result)


@pytest.mark.asyncio
async def test_token_dm_consent_send_projection_and_archive(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'mcp-dm-alice')
    bob_key, bob, _ = await register(app, 'mcp-dm-bob')
    eve_key, eve, _ = await register(app, 'mcp-dm-eve')
    alice_token = await scoped_token(app, alice_key, alice, DM_OPERATIONS | {'discussion.reply@1'})
    bob_token = await scoped_token(app, bob_key, bob, DM_OPERATIONS)
    requested = await dm_call(
        app,
        'communication.dm_request',
        {'recipient': bob, 'introduction': 'Please discuss.'},
        alice,
        alice_token,
        version=3,
    )
    assert requested.status == 'ok' and requested.data['state'] == 'pending', wire(requested)
    topic = requested.data['conversation_id']
    intro = requested.data['introduction_ref']['id']
    pending = await dm_call(
        app,
        'communication.dm_send',
        {'conversation_id': topic, 'body': 'too soon'},
        alice,
        alice_token,
        version=2,
    )
    assert_denied(pending, 'dm_not_active', 'dm_controlled_resource')
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': intro}, 'body': 'bypass pending consent'},
        subject=alice,
        token=alice_token,
    )
    assert_denied(reply, 'dm_controlled_resource')
    initiator_decision = await dm_call(
        app, 'communication.dm_accept', {'conversation_id': topic}, alice, alice_token, version=2
    )
    assert_denied(initiator_decision, 'dm_recipient_required')
    projected = await dm_call(
        app, 'communication.conversation_get', {'id': intro}, bob, bob_token, version=1
    )
    assert projected.status == 'ok', wire(projected)
    assert dict(projected.data) == {
        'topic': {'id': topic, 'revision': None},
        'kind': 'direct',
        'state': 'pending',
        'other_subject': alice,
        'initiator': alice,
    }
    accepted = await dm_call(
        app, 'communication.dm_accept', {'conversation_id': topic}, bob, bob_token, version=2
    )
    assert accepted.status == 'ok' and accepted.data['state'] == 'active', wire(accepted)
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': intro}, 'body': 'bypass the direct send contract'},
        subject=alice,
        token=alice_token,
    )
    assert_denied(reply, 'dm_controlled_resource')
    sent = await dm_call(
        app,
        'communication.dm_send',
        {'conversation_id': topic, 'body': 'accepted hello'},
        alice,
        alice_token,
        version=2,
        rid='mcp-dm-send-once',
    )
    assert sent.status == 'ok', wire(sent)
    replay = await dm_call(
        app,
        'communication.dm_send',
        {'conversation_id': topic, 'body': 'accepted hello'},
        alice,
        alice_token,
        version=2,
        rid='mcp-dm-send-once',
    )
    assert replay.status == 'ok' and replay.replayed and replay.resources == sent.resources
    for rid in (topic, intro, sent.resources[0].id):
        denied = await call(
            app, 'communication.conversation_get', {'id': rid}, key=eve_key, subject=eve
        )
        assert_denied(denied, 'permission_denied')
    archived = await call(
        app, 'communication.dm_archive', {'conversation_id': topic}, key=alice_key, subject=alice
    )
    assert archived.status == 'ok', wire(archived)
    listed = await call(app, 'communication.dm_list', {}, key=alice_key, subject=alice)
    assert listed.status == 'ok' and not listed.data['items'], wire(listed)
    archived_view = await dm_call(
        app, 'communication.conversation_get', {'id': topic}, alice, alice_token, version=1
    )
    assert archived_view.status == 'ok' and archived_view.data['kind'] == 'direct', wire(
        archived_view
    )
    assert archived_view.data['state'] == 'active' and archived_view.data['other_subject'] == bob
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT COUNT(*) FROM resources WHERE parent=? AND type=?', (topic, 'post'))[0]
            == 2
        )


@pytest.mark.asyncio
async def test_token_dm_reject_and_block_keep_state_fences(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'mcp-reject-a')
    bob_key, bob, _ = await register(app, 'mcp-reject-b')
    charlie_key, charlie, _ = await register(app, 'mcp-reject-c')
    alice_token = await scoped_token(app, alice_key, alice, DM_OPERATIONS)
    bob_token = await scoped_token(app, bob_key, bob, DM_OPERATIONS)
    requested = await dm_call(
        app, 'communication.dm_request', {'recipient': bob}, alice, alice_token, version=3
    )
    topic = requested.data['conversation_id']
    rejected = await dm_call(
        app, 'communication.dm_reject', {'conversation_id': topic}, bob, bob_token, version=2
    )
    assert rejected.status == 'ok' and rejected.data['state'] == 'rejected', wire(rejected)
    assert_denied(
        await dm_call(
            app, 'communication.dm_accept', {'conversation_id': topic}, bob, bob_token, version=2
        ),
        'dm_request_closed',
    )
    assert_denied(
        await dm_call(
            app, 'communication.dm_request', {'recipient': bob}, alice, alice_token, version=3
        ),
        'dm_rejected',
    )
    assert_denied(
        await dm_call(
            app,
            'communication.dm_send',
            {'conversation_id': topic, 'body': 'rejected'},
            alice,
            alice_token,
            version=2,
        ),
        'dm_not_active',
        'dm_controlled_resource',
    )
    requested = await dm_call(
        app, 'communication.dm_request', {'recipient': charlie}, alice, alice_token, version=3
    )
    topic = requested.data['conversation_id']
    accepted = await call(
        app,
        'communication.dm_accept',
        {'conversation_id': topic},
        key=charlie_key,
        subject=charlie,
        contract_version=2,
    )
    assert accepted.status == 'ok', wire(accepted)
    blocked = await call(
        app, 'communication.dm_block', {'subject_id': alice}, key=charlie_key, subject=charlie
    )
    assert blocked.status == 'ok', wire(blocked)
    assert_denied(
        await dm_call(
            app,
            'communication.dm_send',
            {'conversation_id': topic, 'body': 'blocked'},
            alice,
            alice_token,
            version=2,
        ),
        'dm_blocked',
    )
    assert_denied(
        await dm_call(
            app, 'communication.dm_request', {'recipient': charlie}, alice, alice_token, version=3
        ),
        'dm_blocked',
    )


@pytest.mark.asyncio
async def test_legacy_dm_versions_still_require_signatures(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'mcp-old-dm')
    _other_key, other, _ = await register(app, 'mcp-old-other')
    vectors = (
        ('communication.dm_request', 1, {'recipient': other}),
        ('communication.dm_request', 2, {'recipient': other, 'introduction': 'old'}),
        ('communication.dm_send', 1, {'conversation_id': '/main', 'body': 'old'}),
        ('communication.dm_accept', 1, {'conversation_id': '/main'}),
        ('communication.dm_reject', 1, {'conversation_id': '/main'}),
    )
    token = await scoped_token(
        app, key, subject, {f'{name}@{version}' for name, version, _ in vectors}
    )
    for name, version, arguments in vectors:
        spec = app.registry.operation(name, version)
        assert spec.require_signature
        assert_denied(
            await dm_call(app, name, arguments, subject, token, version=version),
            'signature_required',
        )
    for name, version in (
        ('communication.dm_request', 3),
        ('communication.dm_send', 2),
        ('communication.dm_accept', 2),
        ('communication.dm_reject', 2),
    ):
        assert not app.registry.operation(name, version).require_signature


@pytest.mark.asyncio
async def test_new_dm_versions_accept_signatures(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'mcp-signed-a')
    bob_key, bob, _ = await register(app, 'mcp-signed-b')
    requested = await call(
        app,
        'communication.dm_request',
        {'recipient': bob},
        key=alice_key,
        subject=alice,
        contract_version=3,
    )
    assert requested.status == 'ok', wire(requested)
    topic = requested.data['conversation_id']
    accepted = await call(
        app,
        'communication.dm_accept',
        {'conversation_id': topic},
        key=bob_key,
        subject=bob,
        contract_version=2,
    )
    assert accepted.status == 'ok', wire(accepted)
    sent = await call(
        app,
        'communication.dm_send',
        {'conversation_id': topic, 'body': 'signed v2'},
        key=alice_key,
        subject=alice,
        contract_version=2,
    )
    assert sent.status == 'ok', wire(sent)


@pytest.mark.asyncio
async def test_token_exact_version_and_readonly_ceiling(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'mcp-version-a')
    _bob_key, bob, _ = await register(app, 'mcp-version-b')
    token = await scoped_token(app, alice_key, alice, {'communication.dm_request@2'})
    denied = await dm_call(
        app, 'communication.dm_request', {'recipient': bob}, alice, token, version=3
    )
    assert_denied(denied, 'credential_ceiling')
    reads = await scoped_token(app, alice_key, alice, {'communication.conversation_get@1'})
    for name, version, arguments in (
        ('communication.dm_request', 3, {'recipient': bob}),
        ('communication.dm_send', 2, {'conversation_id': '/main', 'body': 'no write'}),
        ('communication.dm_accept', 2, {'conversation_id': '/main'}),
        ('communication.dm_reject', 2, {'conversation_id': '/main'}),
    ):
        assert_denied(
            await dm_call(app, name, arguments, alice, reads, version=version), 'credential_ceiling'
        )


@pytest.mark.asyncio
async def test_token_wrong_subject_and_current_parent_revocation(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'mcp-parent-a')
    _bob_key, bob, _ = await register(app, 'mcp-parent-b')
    token = await scoped_token(app, alice_key, alice, DM_OPERATIONS)
    assert_denied(
        await dm_call(app, 'communication.dm_request', {'recipient': alice}, bob, token, version=3),
        'delegation_required',
    )
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(alice_key.key_id)
        owner = await tx.subject(alice)
        await tx.save_credential(replace(credential, revoked_at=NOW), owner.auth_version)
    assert_denied(
        await dm_call(app, 'communication.dm_request', {'recipient': bob}, alice, token, version=3),
        'invalid_grant',
    )


@pytest.mark.asyncio
async def test_conversation_projection_authorizes_post_and_topic_without_content(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'mcp-projection-a')
    bob_key, bob, _ = await register(app, 'mcp-projection-b')
    created = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'mcp-private'},
        key=alice_key,
        subject=alice,
    )
    assert created.status == 'ok', wire(created)
    topic = created.resources[0].id
    private = await call(
        app,
        'content.chmod',
        {'id': topic, 'mode': '0700'},
        key=alice_key,
        subject=alice,
        expected=((topic, created.data['generation']),),
    )
    assert private.status == 'ok', wire(private)
    posted = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'private body'},
        key=alice_key,
        subject=alice,
    )
    assert posted.status == 'ok', wire(posted)
    post = posted.resources[0].id
    for rid in (topic, post):
        projected = await call(
            app, 'communication.conversation_get', {'id': rid}, key=alice_key, subject=alice
        )
        assert projected.status == 'ok', wire(projected)
        assert dict(projected.data) == {
            'topic': {'id': topic, 'revision': None},
            'kind': 'discussion',
        }
        assert_denied(
            await call(
                app, 'communication.conversation_get', {'id': rid}, key=bob_key, subject=bob
            ),
            'permission_denied',
        )
    exact_post = await scoped_token(
        app, alice_key, alice, {'communication.conversation_get@1'}, scope=Scope(resource_id=post)
    )
    assert_denied(
        await dm_call(
            app, 'communication.conversation_get', {'id': post}, alice, exact_post, version=1
        ),
        'credential_ceiling',
    )


@pytest.mark.asyncio
async def test_dm_token_scope_covers_the_conversation_and_request_rolls_back(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'mcp-scope-a')
    bob_key, bob, _ = await register(app, 'mcp-scope-b')
    alice_token = await scoped_token(
        app, alice_key, alice, DM_OPERATIONS, scope=Scope(resource_id=alice)
    )
    denied = await dm_call(
        app,
        'communication.dm_request',
        {'recipient': bob, 'introduction': 'scope denied'},
        alice,
        alice_token,
        version=3,
    )
    assert_denied(denied, 'credential_ceiling')
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM dm_conversations')[0] == 0
    requested = await call(
        app, 'communication.dm_request', {'recipient': bob}, key=alice_key, subject=alice
    )
    assert requested.status == 'ok', wire(requested)
    topic = requested.data['conversation_id']
    assert_denied(
        await dm_call(
            app, 'communication.dm_request', {'recipient': bob}, alice, alice_token, version=3
        ),
        'credential_ceiling',
    )
    bob_token = await scoped_token(app, bob_key, bob, DM_OPERATIONS, scope=Scope(resource_id=bob))
    for operation in ('communication.dm_accept', 'communication.dm_reject'):
        assert_denied(
            await dm_call(app, operation, {'conversation_id': topic}, bob, bob_token, version=2),
            'credential_ceiling',
        )
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT state FROM dm_conversations WHERE resource_id=?', (topic,))[0]
            == 'pending'
        )


@pytest.mark.asyncio
async def test_request_v3_long_active_message_and_new_invitation_limits(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'mcp-long-a')
    bob_key, bob, _ = await register(app, 'mcp-long-b')
    _charlie_key, charlie, _ = await register(app, 'mcp-long-c')
    alice_token = await scoped_token(app, alice_key, alice, DM_OPERATIONS)
    bob_token = await scoped_token(app, bob_key, bob, DM_OPERATIONS)
    requested = await dm_call(
        app,
        'communication.dm_request',
        {'recipient': bob, 'introduction': 'One invitation.'},
        alice,
        alice_token,
        version=3,
    )
    assert requested.status == 'ok', wire(requested)
    topic = requested.data['conversation_id']
    long_body = 'Long active message. ' * 300
    pending = await dm_call(
        app,
        'communication.dm_request',
        {'recipient': bob, 'introduction': long_body},
        alice,
        alice_token,
        version=3,
    )
    assert pending.status == 'ok' and pending.data['state'] == 'pending', wire(pending)
    assert 'introduction_ref' not in pending.data
    accepted = await dm_call(
        app, 'communication.dm_accept', {'conversation_id': topic}, bob, bob_token, version=2
    )
    assert accepted.status == 'ok', wire(accepted)
    active = await dm_call(
        app,
        'communication.dm_request',
        {'recipient': bob, 'introduction': long_body},
        alice,
        alice_token,
        version=3,
    )
    assert active.status == 'ok' and active.data['state'] == 'active', wire(active)
    assert 'introduction_ref' not in active.data
    sent = await dm_call(
        app,
        'communication.dm_send',
        {'conversation_id': topic, 'body': long_body},
        alice,
        alice_token,
        version=2,
    )
    assert sent.status == 'ok', wire(sent)
    read = await call(app, 'discovery.get', {'id': sent.resources[0].id}, key=bob_key, subject=bob)
    assert read.status == 'ok' and read.data['content'] == long_body, wire(read)
    for excessive in ('x' * 501, '界' * 350):
        failed = await dm_call(
            app,
            'communication.dm_request',
            {'recipient': charlie, 'introduction': excessive},
            alice,
            alice_token,
            version=3,
        )
        assert_denied(failed, 'introduction_too_large')
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM dm_conversations')[0] == 1
            assert (
                tx.one('SELECT COUNT(*) FROM resources WHERE parent=? AND type=?', (topic, 'post'))[
                    0
                ]
                == 2
            )
    oversized_active = await dm_call(
        app,
        'communication.dm_request',
        {'recipient': bob, 'introduction': '界' * 11000},
        alice,
        alice_token,
        version=3,
    )
    assert_denied(oversized_active, 'introduction_too_large')
