"""A stale unified patch cannot relocate to a different identical old block."""

import pytest
from test_service import call, register
from test_text_patch import content, state

from msg.core.codec import wire
from msg.core.errors import Failure
from msg.core.text_patch import apply_patch

PATCH = {'kind': 'unified', 'diff': '@@ -1 +1 @@\n-old\n+new\n'}


def test_unified_rebase_requires_unique_anchor_in_base_too():
    base = 'old\nseparator\nold\n'
    current = 'changed\nseparator\nold\n'
    assert apply_patch(base, PATCH) == 'new\nseparator\nold\n'
    with pytest.raises(Failure) as exc:
        apply_patch(current, PATCH, base_source=base)
    assert exc.value.code == 'patch_ambiguous'


@pytest.mark.asyncio
async def test_signed_rebase_cannot_edit_surviving_duplicate(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'rebase-identity')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'old\nseparator\nold\n'},
        key=key,
        subject=owner,
    )
    rid = created.resources[0].id
    edited = await call(
        app,
        'content.text_patch',
        {
            'id': rid,
            'base_revision': created.resources[0].revision,
            'exact': 'old\nseparator',
            'replacement': 'changed\nseparator',
        },
        key=key,
        subject=owner,
        expected=((rid, created.data['generation']),),
    )
    assert edited.status == 'ok', wire(edited)
    before = await state(app, rid)
    result = await call(
        app,
        'content.text_patch',
        {
            'id': rid,
            'base_revision': created.resources[0].revision,
            'base_generation': created.data['generation'],
            'rebase': True,
            'patch': PATCH,
        },
        contract_version=3,
        key=key,
        subject=owner,
        expected=((rid, edited.data['generation']),),
    )
    assert result.status == 'error' and result.error.code == 'patch_ambiguous', wire(result)
    assert await state(app, rid) == before
    assert (await content(app, rid))[1] == 'changed\nseparator\nold\n'
