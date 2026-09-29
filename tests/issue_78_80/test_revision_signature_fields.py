import pytest

from msg.client_content import signed_patch_arguments
from msg.core.errors import Failure
from msg.core.models import ResourceRef


async def _signed_patch(h, resource):
    async with h.app.metadata.transaction(write=False) as tx:
        old = await tx.revision(ResourceRef(id=resource.id))
    return signed_patch_arguments(
        h.signer,
        resource,
        old,
        'old\n',
        {'kind': 'exact', 'exact': 'old', 'replacement': 'new'},
        subject='u_alice',
        created_at=h.ctx.now,
    )


async def _snapshot(h):
    async with h.app.metadata.transaction(write=False) as tx:
        return (
            tx.rows('SELECT id,revision,generation FROM resources ORDER BY id'),
            tx.rows('SELECT id FROM revisions ORDER BY id'),
        )


@pytest.mark.parametrize(
    'dropped,code',
    [
        (('content_signature',), 'content_signature_required'),
        (('content_signature', 'revision_id'), 'content_signature_required'),
        (('content_signature', 'content_created_at'), 'content_signature_required'),
        (('revision_id',), 'revision_id_required'),
    ],
)
async def test_patch_rejects_partial_revision_signature_fields(harness, dropped, code):
    h = harness
    resource = await h.create('r_partial', type='file', body='old\n')
    args = await _signed_patch(h, resource)
    for name in dropped:
        del args[name]
    before = await _snapshot(h)
    with pytest.raises(Failure) as exc:
        await h.invoke(
            'content.text_patch', args, 3, expected=((resource.id, resource.generation),)
        )
    assert exc.value.code == code
    assert await _snapshot(h) == before


async def test_post_write_rejects_unsigned_client_revision_id(harness, monkeypatch):
    h = harness
    post = await h.create('r_post', type='post', body='old\n')
    before = await _snapshot(h)

    async def unexpected(*args, **kwargs):
        pytest.fail('rejected revision fields must not write content')

    monkeypatch.setattr(h.app.contents, 'put_bytes', unexpected)
    with pytest.raises(Failure) as exc:
        await h.invoke(
            'content.post_write',
            {
                'id': post.id,
                'expected_revision': post.revision,
                'body': 'new\n',
                'revision_id': 'v_client_chosen',
            },
            expected=((post.id, post.generation),),
        )
    assert exc.value.code == 'content_signature_required'
    assert await _snapshot(h) == before


async def test_unsigned_and_fully_signed_patches_still_publish(harness):
    h = harness
    plain = await h.create('r_plain', type='file', body='old\n')
    result = await h.invoke(
        'content.text_patch',
        {
            'id': plain.id,
            'base_revision': plain.revision,
            'base_generation': plain.generation,
            'patch': {'kind': 'exact', 'exact': 'old', 'replacement': 'new'},
        },
        3,
        expected=((plain.id, plain.generation),),
    )
    signed = await h.create('r_signed', type='file', body='old\n')
    args = await _signed_patch(h, signed)
    signed_result = await h.invoke(
        'content.text_patch', args, 3, expected=((signed.id, signed.generation),)
    )
    async with h.app.metadata.transaction(write=False) as tx:
        assert (await tx.revision(result.resources[0])).signature is None
        revision = await tx.revision(signed_result.resources[0])
        assert revision.id == args['revision_id'] and revision.signature is not None
