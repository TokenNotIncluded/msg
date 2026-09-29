from datetime import timedelta

import pytest
from test_service import NOW, call, register

from msg.core.codec import wire
from msg.core.requests import request_for


def packet(app, key, uid, operation, args, id):
    return wire(
        request_for(
            operation,
            args,
            app.settings.service_url,
            subject=uid,
            signer=key,
            request_id=id,
            expires_at=NOW + timedelta(seconds=90),
        )
    )


@pytest.mark.asyncio
async def test_atomic_batch_rolls_back_all_children_and_results(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'batch-agent')
    ops = [
        packet(
            app,
            key,
            uid,
            'content.post_create',
            {'parent': '/main', 'body': 'must roll back'},
            'child-a',
        ),
        packet(
            app,
            key,
            uid,
            'content.post_create',
            {'parent': '/private', 'body': 'denied'},
            'child-b',
        ),
    ]
    result = await call(app, 'batch.atomic', {'requests': ops}, key=key, subject=uid)
    assert result.status == 'error' and result.error.code == 'batch_aborted', result
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
        assert tx.one('SELECT COUNT(*) FROM results WHERE request_id=?', ('child-a',))[0] == 0
    ops[1] = packet(
        app, key, uid, 'content.post_create', {'parent': '/main', 'body': 'second'}, 'child-b'
    )
    success = await call(
        app, 'batch.atomic', {'requests': ops}, key=key, subject=uid, rid='batch-good'
    )
    assert success.status == 'ok', success
    repeat = await call(
        app, 'batch.atomic', {'requests': ops}, key=key, subject=uid, rid='batch-good'
    )
    assert repeat.replayed
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 2


@pytest.mark.asyncio
async def test_independent_batch_commits_good_child_and_preserves_error(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'independent-agent')
    ops = [
        packet(
            app,
            key,
            uid,
            'content.post_create',
            {'parent': '/main', 'body': 'committed'},
            'ind-child-a',
        ),
        packet(
            app,
            key,
            uid,
            'content.post_create',
            {'parent': '/private', 'body': 'denied'},
            'ind-child-b',
        ),
    ]
    result = await call(
        app, 'batch.independent', {'requests': ops}, key=key, subject=uid, rid='ind-batch'
    )
    assert result.status == 'ok', result
    assert [r['status'] for r in result.data['results']] == ['ok', 'error']
    repeat = await call(
        app, 'batch.independent', {'requests': ops}, key=key, subject=uid, rid='ind-batch'
    )
    assert repeat.replayed
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 1


@pytest.mark.asyncio
async def test_batch_refuses_nested_and_secret_delivery_before_any_effect(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'no-nest-agent')
    ops = [
        packet(
            app, key, uid, 'content.post_create', {'parent': '/main', 'body': 'no effect'}, 'before'
        ),
        packet(app, key, uid, 'batch.atomic', {'requests': []}, 'nested'),
    ]
    result = await call(app, 'batch.atomic', {'requests': ops}, key=key, subject=uid)
    assert result.error.code == 'operation_not_batchable'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
