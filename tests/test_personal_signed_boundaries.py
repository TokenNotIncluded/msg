"""Independent statements keep historical proof without retaining current authority."""

from dataclasses import replace

import pytest
from read_only_evidence import business_snapshot, readonly_evidence
from test_legacy_directive import signed_will
from test_personal_content_signatures import signed_arguments
from test_service import NOW, call, register

from msg.core.codec import canonical, wire
from msg.security.crypto import verify

KINDS = ['soul', 'agents', 'note', 'legacy']


async def statement(app, key, owner, kind, body, *, ref=None):
    options = {
        'resource_id': ref.id if ref else None,
        'parent_revision': ref.revision if ref else None,
    }
    if kind == 'legacy':
        args, manifest = await signed_will(
            app, key, owner, final_message=body, visibility='private', **options
        )
        return 'identity.legacy_put', args, manifest
    args, manifest = await signed_arguments(app, key, owner, kind=kind, body=body, **options)
    if kind == 'note':
        args.pop('kind')
        args['name'] = 'private-plan'
        return 'identity.note_put', args, manifest
    return 'identity.personal_put', args, manifest


async def saved_statement(app, key, owner, kind):
    op, args, manifest = await statement(app, key, owner, kind, 'privatehistoryneedle first')
    saved = await call(app, op, args, key=key, subject=owner, contract_version=2)
    assert saved.status == 'ok', wire(saved)
    return op, saved, manifest


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', KINDS)
async def test_statement_history_is_private_and_provable_after_signing_key_revocation(
    installed, monkeypatch, kind
):
    app, _ = installed
    key, owner, _ = await register(app, 'statement-owner')
    other_key, other, _ = await register(app, 'statement-outsider')
    op, first, original_manifest = await saved_statement(app, key, owner, kind)
    ref = first.resources[0]
    async with app.metadata.transaction(write=False) as tx:
        original = await tx.revision(ref)
        original_bytes = await app.contents.read_bytes(original.content)
        assert (await tx.resource(ref.id)).mode == 0o600
        verify(key.public_key, canonical(original_manifest), original.signature, purpose='revision')
    _, args, new_manifest = await statement(
        app, key, owner, kind, 'privatehistoryneedle second', ref=ref
    )
    second = await call(
        app,
        op,
        args,
        key=key,
        subject=owner,
        expected=((ref.id, first.data['generation']),),
        contract_version=2,
    )
    assert second.status == 'ok', wire(second)
    async with app.metadata.transaction(write=False) as tx:
        current = await tx.revision(second.resources[0])
        verify(key.public_key, canonical(new_manifest), current.signature, purpose='revision')
        assert current.parents == (ref.revision,)
        assert await tx.revision(ref) == original
    queries = [
        {'id': ref.id},
        {'id': ref.id, 'view': 'meta'},
        {'id': ref.id, 'view': 'history'},
        {'id': ref.id, 'revision': ref.revision},
    ]
    async with readonly_evidence(app, monkeypatch):
        for query in queries:
            own = await call(app, 'discovery.get', query, key=key, subject=owner)
            assert own.status == 'ok', wire(own)
            for reader, subject in [(None, None), (other_key, other)]:
                denied = await call(app, 'discovery.get', query, key=reader, subject=subject)
                assert denied.status == 'error' and denied.error.code == 'permission_denied'
                assert 'privatehistoryneedle' not in str(wire(denied))
        found = await call(
            app,
            'discovery.search',
            {'query': 'privatehistoryneedle'},
            key=other_key,
            subject=other,
        )
        assert found.status == 'ok' and ref.id not in str(found.data)
        assert 'privatehistoryneedle' not in str(found.data)
    _, pending, _ = await statement(
        app, key, owner, kind, 'third statement', ref=second.resources[0]
    )
    async with app.metadata.transaction(write=True) as tx:
        await tx.put(replace(await tx.credential(key.key_id), revoked_at=NOW))
    before = await business_snapshot(app)
    denied = await call(
        app,
        op,
        pending,
        key=key,
        subject=owner,
        expected=((ref.id, second.data['generation']),),
        contract_version=2,
    )
    assert denied.status == 'error' and denied.error.code == 'credential_revoked'
    async with readonly_evidence(app, monkeypatch):
        for query in queries:
            denied = await call(app, 'discovery.get', query, key=key, subject=owner)
            assert denied.status == 'error' and denied.error.code == 'credential_revoked'
    assert await business_snapshot(app) == before
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.revision(ref) == original
        assert await app.contents.read_bytes(original.content) == original_bytes
        verify(key.public_key, canonical(original_manifest), original.signature, purpose='revision')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('fault', ['body_tamper', 'foreign_content_key'])
async def test_invalid_statement_content_proof_rolls_back_every_authoritative_fact(
    installed, kind, fault
):
    app, _ = installed
    key, owner, _ = await register(app, 'proof-owner')
    foreign_key, _, _ = await register(app, 'proof-foreign')
    op, first, _ = await saved_statement(app, key, owner, kind)
    _, args, manifest = await statement(
        app, key, owner, kind, 'signed update', ref=first.resources[0]
    )
    if fault == 'body_tamper':
        args['final_message' if kind == 'legacy' else 'body'] = 'changed after signing'
        code = 'invalid_signature'
    else:
        args['content_signature'] = wire(foreign_key.sign(canonical(manifest), purpose='revision'))
        code = 'content_signer_mismatch'
    before = await business_snapshot(app)
    result = await call(
        app,
        op,
        args,
        key=key,
        subject=owner,
        expected=((first.resources[0].id, first.data['generation']),),
        contract_version=2,
    )
    assert result.status == 'error' and result.error.code == code, wire(result)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_note_archive_restore_preserves_independent_revision_and_content(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'signed-note-lifecycle')
    _, saved, manifest = await saved_statement(app, key, owner, 'note')
    ref = saved.resources[0]
    async with app.metadata.transaction(write=False) as tx:
        original = await tx.revision(ref)
        proof = tx.one(
            'SELECT signature,signed_envelope FROM personal_revision_proofs WHERE revision_id=?',
            (ref.revision,),
        )
    generation = saved.data['generation']
    transitions = [('identity.note_archive', 'archived'), ('identity.note_restore', 'active')]
    for operation, state in transitions:
        result = await call(
            app,
            operation,
            {'name': 'private-plan', 'expected_revision': ref.revision},
            key=key,
            subject=owner,
            expected=((ref.id, generation),),
        )
        assert result.status == 'ok' and result.data['state'] == state, wire(result)
        generation = result.data['generation']
        async with app.metadata.transaction(write=False) as tx:
            assert await tx.revision(ref) == original
            assert (await tx.resource(ref.id)).revision == ref.revision
            assert (
                tx.one(
                    'SELECT signature,signed_envelope FROM personal_revision_proofs WHERE revision_id=?',
                    (ref.revision,),
                )
                == proof
            )
            assert await app.contents.read_bytes(original.content) == b'privatehistoryneedle first'
            verify(key.public_key, canonical(manifest), original.signature, purpose='revision')
