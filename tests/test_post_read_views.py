"""Old browser grants can read public records without gaining private access."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_oauth import browser_login, oauth as oauth
from test_post_actions import action, post
from test_service import call

from msg.core.codec import wire
from msg.core.requests import request_for
from msg.security.oauth import OAuthService


async def restrict(oauth, cookie, operations):
    app, _, subject, _ = oauth
    async with app.metadata.transaction(write=True) as tx:
        _, credential_id, secret = await OAuthService(app).browser_credentials(tx, cookie)
        credential = await tx.credential(credential_id)
        restricted = replace(
            credential,
            ceiling=tuple(
                replace(
                    grant,
                    operations=frozenset(
                        op for op in grant.operations if op.partition('@')[0] not in operations
                    ),
                )
                for grant in credential.ceiling
            ),
        )
        await tx.save_credential(restricted, (await tx.subject(subject)).auth_version)
    return credential_id, secret, restricted


async def private(app, rid, mode=0o700):
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(rid)
        await tx.replace(
            replace(resource, mode=mode, generation=resource.generation + 1), resource.generation
        )


@pytest.mark.asyncio
async def test_old_browser_reads_only_public_forks_and_keeps_signed_ceiling(oauth):
    app, key, subject, http = oauth
    source, _ = await post(oauth)
    branches = []
    for name in ('public-branch.md', 'private-branch.md'):
        result = await call(
            app,
            'discussion.fork',
            {'target': wire(source), 'name': name, 'body': 'A branch'},
            key=key,
            subject=subject,
        )
        assert result.status == 'ok', wire(result)
        branches.append(result.resources[0])
    await private(app, branches[1].id)
    directory = await call(
        app, 'file.mkdir', {'parent': '/main', 'name': 'hidden-parent'}, key=key, subject=subject
    )
    assert directory.status == 'ok', wire(directory)
    hidden = await call(
        app,
        'discussion.fork',
        {
            'target': wire(source),
            'parent': directory.resources[0].id,
            'name': 'hidden-ancestor.md',
            'body': 'Hidden by parent',
        },
        key=key,
        subject=subject,
    )
    assert hidden.status == 'ok', wire(hidden)
    await private(app, directory.resources[0].id)
    cookie = await browser_login(oauth)
    token_id, secret, restricted = await restrict(
        oauth, cookie, {'discussion.forks', 'discussion.fork'}
    )
    result = await http.get('/_post/forks', params={'id': source.id})
    assert result.status_code == 200, result.text
    assert [item['id'] for item in result.json()['data']['items']] == [branches[0].id]
    for omitted in (
        branches[1].id,
        hidden.resources[0].id,
        'hidden-ancestor.md',
        'private-branch.md',
    ):
        assert omitted not in result.text
    html = await http.get('/_post/forks', params={'id': source.id}, headers={'Accept': 'text/html'})
    assert html.status_code == 200 and html.headers['content-type'].startswith('text/html')
    assert 'public-branch.md' in html.text and '当前仅显示公开可读' in html.text
    head = await http.head(
        '/_post/forks', params={'id': source.id}, headers={'Accept': 'text/html'}
    )
    assert not head.content and head.headers['content-length'] == str(len(html.content))
    explicit = await call(
        app, 'discussion.forks', {'id': source.id}, subject=subject, token=(token_id, secret)
    )
    assert explicit.error.code == 'credential_ceiling'
    signed_api = await http.post(
        '/-/p/discussion.forks',
        json=wire(
            request_for(
                'discussion.forks',
                {'id': source.id},
                app.settings.service_url,
                subject=subject,
                token=(token_id, secret),
                expires_at=app.clock() + timedelta(minutes=3),
            )
        ),
    )
    assert (
        signed_api.status_code == 403 and signed_api.json()['error']['code'] == 'credential_ceiling'
    )
    denied = await action(
        oauth, 'discussion.fork', source.id, revision=source.revision, body='Denied branch'
    )
    assert denied.status_code == 403 and denied.json()['error']['code'] == 'credential_ceiling'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.credential(token_id)).ceiling == restricted.ceiling
    await private(app, directory.resources[0].id, mode=0o711)
    readable = await http.get('/_post/forks', params={'id': source.id})
    assert hidden.resources[0].id in {item['id'] for item in readable.json()['data']['items']}
    await private(app, source.id)
    denied = await http.get(
        '/_post/forks', params={'id': source.id}, headers={'Accept': 'text/html'}
    )
    assert denied.status_code == 403 and '没有读取这些记录' in denied.text
    assert 'public-branch.md' not in denied.text


@pytest.mark.asyncio
async def test_public_state_is_explicitly_unknown_and_proof_views_are_safe(oauth):
    app, _, _, http = oauth
    source, _ = await post(oauth)
    cookie = await browser_login(oauth)
    assert (await action(oauth, 'discussion.bookmark', source.id)).status_code == 200
    note = '<script>alert(1)</script> [unsafe](javascript:alert(2))'
    proved = await action(
        oauth, 'discussion.prove', source.id, revision=source.revision, kind='USED', note=note
    )
    assert proved.status_code == 200, proved.text
    credential_id, _, restricted = await restrict(
        oauth, cookie, {'discussion.state', 'discussion.proofs'}
    )
    state = await http.get('/_post/state', params={'id': source.id, 'revision': source.revision})
    assert state.status_code == 200, state.text
    data = state.json()['data']
    assert data['personal_state_available'] is False
    assert data['bookmarked'] is False and data['my_proofs'] == [] and data['proofs']['USED'] == 1
    params = {'id': source.id, 'revision': source.revision, 'limit': '1'}
    records = await http.get('/_post/proofs', params=params)
    assert records.status_code == 200 and records.json()['data']['items'][0]['note'] == note
    html = await http.get('/_post/proofs', params=params, headers={'Accept': 'text/html'})
    assert html.status_code == 200 and 'USED' in html.text
    assert '<script>alert' not in html.text and 'href="javascript:' not in html.text
    assert '&lt;script&gt;' in html.text
    for accept, extra, media in [
        ('text/markdown', {}, 'text/markdown'),
        ('text/html', {'format': 'raw'}, 'text/plain'),
    ]:
        raw = await http.get(
            '/_post/proofs', params={**params, **extra}, headers={'Accept': accept}
        )
        assert raw.status_code == 200 and raw.headers['content-type'].startswith(media)
        assert '<!doctype' not in raw.text and 'USED' in raw.text
    empty = await http.get(
        '/_post/forks', params={'id': source.id}, headers={'Accept': 'text/html'}
    )
    assert empty.status_code == 200 and '暂无可读的分叉' in empty.text
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.credential(credential_id)).ceiling == restricted.ceiling
    await private(app, source.id)
    for path in ('state', 'proofs'):
        denied = await http.get('/_post/' + path, params={'id': source.id})
        assert denied.status_code == 403 and source.id not in str(denied.json().get('data'))


@pytest.mark.asyncio
async def test_private_bookmarks_never_fall_back_to_public_or_empty(oauth):
    _, _, _, http = oauth
    source, _ = await post(oauth)
    cookie = await browser_login(oauth)
    assert (await action(oauth, 'discussion.bookmark', source.id)).status_code == 200
    await restrict(oauth, cookie, {'discussion.bookmarks'})
    json = await http.get('/bookmarks', headers={'Accept': 'application/json'})
    assert json.status_code == 403 and json.json()['error']['code'] == 'credential_ceiling'
    html = await http.get('/bookmarks', headers={'Accept': 'text/html'})
    assert html.status_code == 403 and html.headers['content-type'].startswith('text/html')
    assert '当前授权未包含读取本人收藏' in html.text and 'button-demo.md' not in html.text
    assert 'No saved posts yet' not in html.text
    head = await http.head('/bookmarks', headers={'Accept': 'text/html'})
    assert not head.content
    head_json = await http.head('/bookmarks', headers={'Accept': 'application/json'})
    assert head_json.status_code == 403 and not head_json.content
