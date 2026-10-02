"""Live activity does not expose unreadable boards or fail their public neighbours."""

from dataclasses import replace

from test_service import call, register


async def test_now_skips_public_post_in_traverse_only_board(installed):
    app, _ = installed
    hidden_key, hidden_author, _ = await register(app, 'traverse-only-author')
    public_key, public_author, _ = await register(app, 'visible-author')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/', 'name': 'traverse-only-board'},
        key=hidden_key,
        subject=hidden_author,
    )
    assert topic.status == 'ok', topic.error
    topic_id = topic.resources[0].id
    hidden = await call(
        app,
        'content.post_create',
        {'parent': topic_id, 'body': 'A public post in an unreadable board'},
        key=hidden_key,
        subject=hidden_author,
    )
    public = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'Visible live activity'},
        key=public_key,
        subject=public_author,
    )
    assert hidden.status == public.status == 'ok'

    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(topic_id)
        await tx.replace(
            replace(resource, mode=0o711, generation=resource.generation + 1),
            resource.generation,
        )

    # Reading a known post only needs traversal through its parent directory.
    readable = await call(app, 'discovery.get', {'id': hidden.resources[0].id})
    assert readable.status == 'ok', readable.error
    unreadable = await call(app, 'discovery.get', {'id': topic_id})
    assert unreadable.status == 'error'
    assert unreadable.error.code == 'permission_denied'

    result = await call(app, 'discovery.now', {})
    assert result.status == 'ok', result.error
    assert {node['id'] for node in result.data['nodes']} == {public_author}
    assert [event['id'] for event in result.data['events']] == [public.resources[0].id]
    assert not result.data['edges']
    assert topic_id not in str(result.data)
    assert 'traverse-only-board' not in str(result.data)
