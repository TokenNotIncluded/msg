"""Post buttons use real browser grants, ACLs and persistent account state."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.codec import wire
from msg.security.browser_actions import BROWSER_POST_WRITES
from msg.security.oauth import OAuthService
from msg.transports.oauth_http import csrf


async def post(oauth):
    app, _, _, _ = oauth
    key, author, _ = await register(app, 'post-author')
    result = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'button-demo.md', 'body': '# Button demo\n\nA readable post.'},
        key=key,
        subject=author,
    )
    assert result.status == 'ok', wire(result)
    return result.resources[0], author


async def action(oauth, operation, resource, **extra):
    app, _, _, http = oauth
    return await http.post(
        '/oauth/post-action',
        json={
            'csrf': csrf(http.cookies.get('msg_session')),
            'id': resource,
            'operation': operation,
            'request_id': uuid4().hex,
            **extra,
        },
        headers={'Origin': app.settings.service_url},
    )


@pytest.mark.asyncio
async def test_post_buttons_persist_and_keep_bookmarks_private(oauth):
    app, key, subject, http = oauth
    resource, author = await post(oauth)
    path = '/_id/' + resource.id
    anonymous = await http.get(path, headers={'Accept': 'text/html'})
    assert anonymous.status_code == 200
    for name in (
        'ACK',
        'USED',
        'VERIFIED',
        'SOLVED',
        'THANKS',
        'fork',
        'bookmark',
        'follow',
        'comment',
    ):
        assert f'data-action="{name}"' in anonymous.text
    assert anonymous.text.index('A readable post.') < anonymous.text.index('id="post-actions"')
    await browser_login(oauth)
    for operation, field in [
        ('discussion.like', 'liked'),
        ('discussion.bookmark', 'bookmarked'),
        ('communication.follow', 'following'),
    ]:
        response = await action(oauth, operation, author if field == 'following' else resource.id)
        assert response.status_code == 200, response.text
        assert response.json()['data'][field] is True
    state = (await http.get('/_post/state', params={'id': resource.id})).json()['data']
    assert {k: state[k] for k in ('likes', 'liked', 'bookmarked', 'following')} == {
        'likes': 1,
        'liked': True,
        'bookmarked': True,
        'following': True,
    }
    assert state['proofs'] == dict.fromkeys(('ACK', 'USED', 'VERIFIED', 'SOLVED', 'THANKS'), 0)
    assert 'button-demo.md' in (await http.get('/bookmarks')).text
    public = await call(app, 'discussion.state', {'id': resource.id})
    assert {k: public.data[k] for k in ('likes', 'liked', 'bookmarked', 'following')} == {
        'likes': 1,
        'liked': False,
        'bookmarked': False,
        'following': False,
    }
    other_key, other, _ = await register(app, 'other-reader')
    saved = await call(app, 'discussion.bookmarks', {}, key=other_key, subject=other)
    assert saved.status == 'ok' and not saved.data['items']
    async with app.metadata.transaction(write=True) as tx:
        original = await tx.resource(resource.id)
        await tx.replace(
            replace(original, mode=0o700, generation=original.generation + 1), original.generation
        )
    hidden = await http.get('/_post/state', params={'id': resource.id})
    assert hidden.status_code == 403
    assert 'button-demo.md' not in (await http.get('/bookmarks')).text
    denied = await action(oauth, 'discussion.bookmark', resource.id)
    assert denied.status_code == 403
    async with app.metadata.transaction(write=True) as tx:
        current = await tx.resource(resource.id)
        await tx.replace(
            replace(current, mode=original.mode, generation=current.generation + 1),
            current.generation,
        )
    for operation, field in [
        ('discussion.unlike', 'liked'),
        ('discussion.unbookmark', 'bookmarked'),
        ('communication.unfollow', 'following'),
    ]:
        response = await action(oauth, operation, author if field == 'following' else resource.id)
        assert response.status_code == 200, response.text
        assert response.json()['data'][field] is False
    assert 'button-demo.md' not in (await http.get('/bookmarks')).text


@pytest.mark.asyncio
async def test_post_action_csrf_allowlist_reply_and_replay(oauth):
    app, _, _, http = oauth
    resource, _ = await post(oauth)
    await browser_login(oauth)
    payload = {
        'csrf': csrf(http.cookies.get('msg_session')),
        'id': resource.id,
        'operation': 'discussion.like',
        'request_id': uuid4().hex,
    }
    assert (await http.post('/oauth/post-action', json=payload)).status_code == 400
    assert (
        await http.post(
            '/oauth/post-action',
            json={**payload, 'csrf': 'bad'},
            headers={'Origin': app.settings.service_url},
        )
    ).status_code == 400
    for operation in ('content.post_create', 'root.provision', {}):
        response = await http.post(
            '/oauth/post-action',
            json={**payload, 'operation': operation},
            headers={'Origin': app.settings.service_url},
        )
        assert response.status_code == 400, response.text
    first = await http.post(
        '/oauth/post-action', json=payload, headers={'Origin': app.settings.service_url}
    )
    replay = await http.post(
        '/oauth/post-action', json=payload, headers={'Origin': app.settings.service_url}
    )
    assert first.status_code == replay.status_code == 200, first.text
    assert replay.json()['replayed'] is True
    response = await action(
        oauth,
        'discussion.reply',
        resource.id,
        body='A browser comment.',
        revision=resource.revision,
    )
    assert response.status_code == 200, response.text
    reply_id = response.json()['resources'][0]['id']
    reply = await http.get('/_id/' + reply_id, headers={'Accept': 'text/html'})
    assert 'A browser comment.' in reply.text and 'id="post-actions"' in reply.text
    raw = await http.get('/_id/' + reply_id + '?format=raw', headers={'Accept': 'text/html'})
    assert 'post-actions' not in raw.text and 'A browser comment.' in raw.text
    own = await http.get('/_id/' + reply_id, headers={'Accept': 'text/html'})
    assert 'data-action="follow"' not in own.text


@pytest.mark.asyncio
async def test_old_read_only_session_does_not_gain_write_permissions(oauth):
    app, _, subject, http = oauth
    resource, _ = await post(oauth)
    cookie = await browser_login(oauth)
    async with app.metadata.transaction(write=True) as tx:
        _, credential_id, _ = await OAuthService(app).browser_credentials(tx, cookie)
        credential = await tx.credential(credential_id)
        await tx.save_credential(
            replace(
                credential,
                ceiling=tuple(
                    replace(
                        grant,
                        operations=frozenset(
                            op
                            for op in grant.operations
                            if op.partition('@')[0] not in BROWSER_POST_WRITES
                        ),
                    )
                    for grant in credential.ceiling
                ),
            ),
            (await tx.subject(subject)).auth_version,
        )
    response = await action(oauth, 'discussion.like', resource.id)
    assert response.status_code == 403 and response.json()['error']['code'] == 'credential_ceiling'
    state = (await http.get('/_post/state', params={'id': resource.id})).json()['data']
    assert state['likes'] == 0 and state['liked'] is False


@pytest.mark.asyncio
async def test_browser_claims_and_fork_are_explicit_revision_writes(oauth):
    app, _, _, http = oauth
    resource, _ = await post(oauth)
    await browser_login(oauth)
    page = await http.get('/_id/' + resource.id, headers={'Accept': 'text/html'})
    assert 'data-action="like"' not in page.text
    for kind in ('ACK', 'USED', 'VERIFIED', 'SOLVED', 'THANKS'):
        response = await action(
            oauth,
            'discussion.prove',
            resource.id,
            revision=resource.revision,
            kind=kind,
            note='Browser claim',
        )
        assert response.status_code == 200, response.text
        assert response.json()['data']['proofs'][kind] == 1
    state = await http.get(
        '/_post/state', params={'id': resource.id, 'revision': resource.revision}
    )
    assert state.status_code == 200, state.text
    assert len(state.json()['data']['my_proofs']) == 5
    records = await http.get(
        '/_post/proofs', params={'id': resource.id, 'revision': resource.revision}
    )
    assert records.status_code == 200, records.text
    assert len(records.json()['data']['items']) == 5
    assert all(record['auth'] == 'token' for record in records.json()['data']['items'])
    branch = await action(
        oauth, 'discussion.fork', resource.id, revision=resource.revision, body='New direction'
    )
    assert branch.status_code == 200, branch.text
    root = await call(app, 'discussion.thread', {'id': branch.json()['resources'][0]['id']})
    assert root.data['root'] != resource.id
    assert len(root.data['items']) == 1
    forks = await http.get('/_post/forks', params={'id': resource.id})
    assert forks.status_code == 200, forks.text
    assert forks.json()['data']['items'][0]['id'] == root.data['root']
