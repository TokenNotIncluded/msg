"""Delivery pagination and board write paths preserve their local boundaries."""

from test_service import call, register


async def test_frozen_board_cannot_be_repopulated_by_move_or_restore(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'frozen-board-owner')
    board = await call(
        app, 'content.topic_create', {'parent': '/', 'name': 'frozen-board'}, key=key, subject=owner
    )
    assert board.status == 'ok', board.error
    board_id = board.resources[0].id
    inside = await call(
        app, 'content.post_create', {'parent': board_id, 'body': 'inside'}, key=key, subject=owner
    )
    outside = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'outside'}, key=key, subject=owner
    )
    assert inside.status == outside.status == 'ok'
    archived = await call(
        app,
        'content.archive',
        {'id': inside.resources[0].id},
        key=key,
        subject=owner,
        expected=((inside.resources[0].id, inside.data['generation']),),
    )
    assert archived.status == 'ok', archived.error
    frozen = await call(
        app,
        'content.topic_configure',
        {'id': board_id, 'policy': {'editable': False}},
        key=key,
        subject=owner,
        expected=((board_id, board.data['generation']),),
    )
    assert frozen.status == 'ok', frozen.error

    for operation, args, expected in (
        ('content.post_create', {'parent': board_id, 'body': 'new'}, ()),
        (
            'content.move',
            {'id': outside.resources[0].id, 'parent': board_id},
            ((outside.resources[0].id, outside.data['generation']),),
        ),
        (
            'content.restore',
            {'id': inside.resources[0].id},
            ((inside.resources[0].id, archived.data['generation']),),
        ),
    ):
        denied = await call(app, operation, args, key=key, subject=owner, expected=expected)
        assert denied.status == 'error', operation
        assert denied.error.code == 'content_frozen', operation

    async with app.metadata.transaction(write=False) as tx:
        outside_resource = await tx.resource(outside.resources[0].id)
        inside_resource = await tx.resource(inside.resources[0].id)
        assert outside_resource.parent != board_id
        assert outside_resource.generation == outside.data['generation']
        assert inside_resource.state == 'archived'
        assert inside_resource.generation == archived.data['generation']

    reopened = await call(
        app,
        'content.topic_configure',
        {'id': board_id, 'policy': {'editable': True}},
        key=key,
        subject=owner,
        expected=((board_id, frozen.data['generation']),),
    )
    assert reopened.status == 'ok', reopened.error
    moved = await call(
        app,
        'content.move',
        {'id': outside.resources[0].id, 'parent': board_id},
        key=key,
        subject=owner,
        expected=((outside.resources[0].id, outside.data['generation']),),
    )
    restored = await call(
        app,
        'content.restore',
        {'id': inside.resources[0].id},
        key=key,
        subject=owner,
        expected=((inside.resources[0].id, archived.data['generation']),),
    )
    assert moved.status == 'ok', moved.error
    assert restored.status == 'ok', restored.error


async def test_drop_pages_keep_boxes_private_and_cancel_is_sender_only(installed):
    app, _ = installed
    sender_key, sender, _ = await register(app, 'drop-page-sender')
    recipient_key, recipient, _ = await register(app, 'drop-page-recipient')
    other_key, other, _ = await register(app, 'drop-page-other')
    delivered = set()
    for index in range(3):
        result = await call(
            app,
            'communication.drop_deposit',
            {'recipient': recipient, 'kind': 'dead_drop', 'body': f'private {index}'},
            key=sender_key,
            subject=sender,
        )
        assert result.status == 'ok', result.error
        delivered.add(result.data['id'])
    unrelated = await call(
        app,
        'communication.drop_deposit',
        {'recipient': other, 'kind': 'dead_drop', 'body': 'unrelated'},
        key=sender_key,
        subject=sender,
    )
    assert unrelated.status == 'ok', unrelated.error

    found = []
    arguments = {'limit': 1}
    while True:
        page = await call(
            app, 'communication.drop_list', arguments, key=recipient_key, subject=recipient
        )
        assert page.status == 'ok', page.error
        assert len(page.data['items']) == 1
        item = page.data['items'][0]
        assert 'body' not in item and 'ciphertext' not in item and 'nonce' not in item
        found.append(item['id'])
        if page.data['after'] is None:
            break
        arguments['after'] = page.data['after']
        assert len(found) < 3
    assert len(found) == len(set(found)) == 3
    assert set(found) == delivered

    outbox = await call(
        app,
        'communication.drop_list',
        {'box': 'outbox'},
        key=recipient_key,
        subject=recipient,
    )
    assert outbox.status == 'ok' and not outbox.data['items']
    cancelled_id = found[0]
    for key, subject in ((recipient_key, recipient), (other_key, other)):
        denied = await call(
            app, 'communication.drop_cancel', {'id': cancelled_id}, key=key, subject=subject
        )
        assert denied.error.code == 'not_found'
    cancelled = await call(
        app,
        'communication.drop_cancel',
        {'id': cancelled_id},
        key=sender_key,
        subject=sender,
    )
    assert cancelled.status == 'ok' and cancelled.data['state'] == 'cancelled'
    rejected = await call(
        app,
        'communication.drop_claim',
        {'id': cancelled_id},
        key=recipient_key,
        subject=recipient,
    )
    assert rejected.error.code == 'drop_cancelled'
