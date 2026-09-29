from test_service import call, register

from msg.core.models import ResourceRef


async def test_post_and_reply_use_markdown_paths_everywhere(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'path-agent')
    post = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'root'}, key=key, subject=subject
    )
    post_id = post.resources[0].id
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': post_id, 'revision': post.resources[0].revision}, 'body': 'reply'},
        key=key,
        subject=subject,
    )
    reply_id = reply.resources[0].id
    async with app.metadata.transaction(write=False) as tx:
        post_path = await tx.path(post_id)
        reply_path = await tx.path(reply_id)
        assert post_path.endswith('.md')
        assert reply_path.endswith('.md')
        assert (await tx.revision(ResourceRef(id=reply_id))).resource_id == reply_id
    found = await call(app, 'discovery.get', {'id': post_id})
    assert found.data['path'] == post_path
