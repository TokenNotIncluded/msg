"""The official content editing check uses authenticated operations and retained history."""

import pytest
from test_service import call, register

from msg.admin.content_edit_check import check_content_editing


@pytest.mark.asyncio
async def test_content_edit_check_executes_patch_and_denial_paths(installed):
    app, _ = installed
    observed = []

    async def invoke(name, args, key=None, subject=None, **kwargs):
        result = await call(app, name, args, key=key, subject=subject, **kwargs)
        observed.append((name, args, result))
        return result

    async def enrolled(name):
        key, subject, _ = await register(app, name)
        return key, subject

    assert await check_content_editing(app, invoke, enrolled, '')
    patches = [result for name, _, result in observed if name == 'file.patch']
    assert len(patches) == 3
    assert patches[0].status == 'ok'
    assert {result.error.code for result in patches[1:]} == {
        'revision_conflict',
        'permission_denied',
    }
    history = [
        result
        for name, args, result in observed
        if name == 'file.read' and args.get('view') == 'history'
    ]
    assert len(history) == 1 and len(history[0].data['revisions']) == 2
