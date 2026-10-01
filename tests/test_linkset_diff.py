"""Stable relation links and revision diffs are authorized read projections."""

import difflib
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


async def business_state(app):
    async with app.metadata.transaction(write=False) as tx:
        return (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM jobs')[0],
            tx.one('SELECT COUNT(*) FROM revisions')[0],
            tx.one('SELECT SUM(generation) FROM resources')[0],
        )


@pytest.mark.asyncio
async def test_linkset_uses_real_refs_and_authorized_collection_pages(installed):
    app, _ = installed
    key, user, _ = await register(app, 'link-owner')
    post = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'root'}, key=key, subject=user
    )
    rid = post.resources[0].id
    replies = []
    for index in range(2):
        reply = await call(
            app,
            'discussion.reply',
            {'target': wire(post.resources[0]), 'body': f'reply {index}'},
            key=key,
            subject=user,
        )
        assert reply.status == 'ok', wire(reply)
        replies.append(reply.resources[0].id)
    quoted = await call(
        app,
        'discussion.quote',
        {'parent': '/main', 'target': wire(post.resources[0]), 'body': 'quote'},
        key=key,
        subject=user,
    )
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@link-owner/files',
            'name': 'attached.txt',
            'data': b64(b'attached bytes'),
            'media_type': 'text/plain',
        },
        key=key,
        subject=user,
    )
    attached = await call(
        app,
        'content.attach',
        {'post': rid, 'source': wire(source.resources[0])},
        key=key,
        subject=user,
        expected=((rid, post.data['generation']),),
    )
    assert attached.status == 'ok', wire(attached)
    before = await business_state(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        long = await http.get(f'/_read/{rid}/links')
        short = await http.get(f'/_r/{rid}/links')
        assert long.status_code == short.status_code == 200, (long.text, short.text)
        assert long.content == short.content
        links = short.json()['links']
        assert links['self']['ref']['id'] == rid
        assert links['self']['path'].endswith('.md')
        own = await http.get(f'/_r/{rid}/l/self')
        assert own.status_code == 200 and own.json() == links['self']
        assert links['t']['ref']['id'] == 't_main' and links['t']['path'] == '/main'
        assert links['a']['ref']['id'] == user
        assert links['c']['path'] == f'/_r/{rid}/l/c'
        assert links['b']['path'] == f'/_r/{rid}/l/b'
        assert links['f']['path'] == f'/_r/{rid}/l/f'
        assert links['d']['path'] == f'/_r/{rid}/l/d'
        projection = await http.get(f'/_r/{rid}/json')
        assert projection.status_code == 200
        assert projection.json()['links']['self']['ref'] == links['self']['ref']
        markdown = await http.get(links['self']['path'])
        assert markdown.status_code == 200
        assert markdown.text.startswith('---\n')
        assert 'channel: "/main"' in markdown.text
        assert 'author: "/@link-owner"' in markdown.text
        page = await http.get(f'/_r/{rid}/l/c?limit=1')
        assert page.status_code == 200, page.text
        assert [item['ref']['id'] for item in page.json()['items']][0] in replies
        assert page.json()['next'].startswith('/_r/c/')
        continued = await http.get(page.json()['next'])
        assert continued.status_code == 200
        ids = {item['ref']['id'] for item in page.json()['items'] + continued.json()['items']}
        assert ids == set(replies)
        backlink = await http.get(f'/_r/{rid}/l/b')
        assert quoted.resources[0].id in {item['ref']['id'] for item in backlink.json()['items']}
        files = await http.get(f'/_r/{rid}/l/f')
        assert attached.data['attachment']['id'] in {
            item['ref']['id'] for item in files.json()['items']
        }
        references = await http.get(f'/_r/{quoted.resources[0].id}/l/q')
        assert rid in {item['ref']['id'] for item in references.json()['items']}
        reply_links = await http.get(f'/_r/{replies[0]}/links')
        assert reply_links.json()['links']['r']['ref']['id'] == rid
        assert reply_links.json()['links']['p']['ref']['id'] == rid
        unknown = await http.get(f'/_r/{rid}/l/z')
        assert unknown.status_code == 400
        assert unknown.json()['error']['code'] == 'unknown_link_relation'
    assert await business_state(app) == before


@pytest.mark.asyncio
async def test_known_previous_and_arbitrary_revision_diffs_are_exact(installed):
    app, _ = installed
    key, user, _ = await register(app, 'diff-owner')
    created = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'before\n'}, key=key, subject=user
    )
    rid = created.resources[0].id
    old = created.resources[0].revision
    edited = await call(
        app,
        'content.post_edit',
        {'id': rid, 'expected_revision': old, 'body': 'after\n'},
        key=key,
        subject=user,
        expected=((rid, created.data['generation']),),
    )
    assert edited.status == 'ok', wire(edited)
    new = edited.resources[0].revision
    before = await business_state(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        current = await http.get(f'/_read/{rid}/diff/{old}')
        short = await http.get(f'/_r/{rid}/diff/{old}')
        arbitrary = await http.get(f'/_r/{rid}/diff/{old}/{new}')
        previous = await http.get(f'/_r/{rid}/l/d')
        assert (
            current.status_code
            == short.status_code
            == arbitrary.status_code
            == previous.status_code
            == 200
        )
        assert (
            current.json()['diff']
            == short.json()['diff']
            == arbitrary.json()['diff']
            == previous.json()['diff']
        )
        assert '-before\n' in current.json()['diff'] and '+after\n' in current.json()['diff']
        assert current.json()['from']['revision'] == old and current.json()['to']['revision'] == new
        assert previous.json()['from']['revision'] == old
        links = await http.get(f'/_r/{rid}/links')
        assert links.json()['links']['d']['path'] == f'/_r/{rid}/l/d'
        history = await http.get(f'/_r/{rid}/l/h?limit=1')
        assert history.status_code == 200 and history.json()['next'].startswith('/_r/c/')
        assert history.json()['items'][0]['author'] == user
        assert history.json()['items'][0]['created_at']
        assert not current.is_redirect and not short.is_redirect
    assert await business_state(app) == before


@pytest.mark.asyncio
async def test_private_links_and_diff_hide_metadata_until_current_authorization(installed):
    app, _ = installed
    key, user, _ = await register(app, 'private-link-owner')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'private-old'},
        key=key,
        subject=user,
    )
    rid = created.resources[0].id
    old = created.resources[0].revision
    edited = await call(
        app,
        'content.post_edit',
        {'id': rid, 'expected_revision': old, 'body': 'private-new'},
        key=key,
        subject=user,
        expected=((rid, created.data['generation']),),
    )
    locked = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=user,
        expected=((rid, edited.data['generation']),),
    )
    assert locked.status == 'ok'
    before = await business_state(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for suffix in ('links', 'l/t', f'diff/{old}', f'diff/{old}/{edited.resources[0].revision}'):
            hidden = await http.get(f'/_r/{rid}/{suffix}')
            assert hidden.status_code == 403
            assert 'private-old' not in hidden.text and 'private-new' not in hidden.text
            head = await http.head(f'/_read/{rid}/{suffix}')
            assert head.status_code == 403
        packet = request_for(
            'discovery.links',
            {'id': rid},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=120),
        )
        allowed = await http.get(
            f'/_r/{rid}/links', headers={'X-Msg-Request': b64(canonical(packet))}
        )
        assert allowed.status_code == 200
        path_packet = request_for(
            'discovery.links',
            {'id': rid},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        path_only = await http.get(f'/_r/{rid}/links/p/' + b64(canonical(path_packet)))
        assert path_only.status_code == 200 and path_only.content == allowed.content
        assert path_only.headers['cache-control'] == 'no-store'
        diff_packet = request_for(
            'discovery.diff_view',
            {'id': rid, 'known_revision': old},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        path_diff = await http.get(f'/_r/{rid}/diff/{old}/p/' + b64(canonical(diff_packet)))
        assert path_diff.status_code == 200
        assert '-private-old' in path_diff.json()['diff']
    assert await business_state(app) == before


@pytest.mark.asyncio
async def test_backlink_collection_does_not_reveal_private_source_or_count(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'visible-target')
    source_key, source, _ = await register(app, 'private-quoter')
    target = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'public target'},
        key=owner_key,
        subject=owner,
    )
    quoted = await call(
        app,
        'discussion.quote',
        {'parent': '/main', 'target': wire(target.resources[0]), 'body': 'hidden quote'},
        key=source_key,
        subject=source,
    )
    qid = quoted.resources[0].id
    locked = await call(
        app,
        'content.chmod',
        {'id': qid, 'mode': '0600'},
        key=source_key,
        subject=source,
        expected=((qid, quoted.data['generation']),),
    )
    assert locked.status == 'ok'
    rid = target.resources[0].id
    before = await business_state(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        links = await http.get(f'/_r/{rid}/links')
        backlinks = await http.get(f'/_r/{rid}/l/b')
        assert links.status_code == backlinks.status_code == 200
        assert 'b' not in links.json()['links']
        assert 'd' not in links.json()['links']
        assert backlinks.json()['items'] == []
        assert qid not in links.text + backlinks.text
    assert await business_state(app) == before


@pytest.mark.asyncio
async def test_comment_cursor_rechecks_visibility_after_revocation(installed):
    app, _ = installed
    key, user, _ = await register(app, 'comment-visibility')
    root = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'root'}, key=key, subject=user
    )
    replies = []
    for index in range(2):
        reply = await call(
            app,
            'discussion.reply',
            {'target': wire(root.resources[0]), 'body': f'comment {index}'},
            key=key,
            subject=user,
        )
        replies.append(reply)
    rid = root.resources[0].id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        first = await http.get(f'/_r/{rid}/l/c?limit=1')
        assert first.status_code == 200 and first.json().get('next')
        first_id = first.json()['items'][0]['ref']['id']
        hidden = next(reply for reply in replies if reply.resources[0].id != first_id)
        locked = await call(
            app,
            'content.chmod',
            {'id': hidden.resources[0].id, 'mode': '0600'},
            key=key,
            subject=user,
            expected=((hidden.resources[0].id, hidden.data['generation']),),
        )
        assert locked.status == 'ok'
        before = await business_state(app)
        next_page = await http.get(first.json()['next'])
        assert next_page.status_code == 200
        assert next_page.json()['items'] == []
        assert hidden.resources[0].id not in next_page.text
        assert await business_state(app) == before


@pytest.mark.asyncio
async def test_large_diff_pages_pin_both_revisions_without_skipping_lines(installed):
    app, _ = installed
    key, user, _ = await register(app, 'diff-pages')
    old_text = ''.join(f'old {index}\n' for index in range(400))
    new_text = ''.join(f'new {index}\n' for index in range(400))
    created = await call(
        app, 'content.post_create', {'parent': '/main', 'body': old_text}, key=key, subject=user
    )
    rid = created.resources[0].id
    old = created.resources[0].revision
    edited = await call(
        app,
        'content.post_edit',
        {'id': rid, 'expected_revision': old, 'body': new_text},
        key=key,
        subject=user,
        expected=((rid, created.data['generation']),),
    )
    new = edited.resources[0].revision
    async with app.metadata.transaction(write=False) as tx:
        canonical_path = await tx.path(rid)
    chunks = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        path = f'/_r/{rid}/diff/{old}/{new}'
        while path:
            response = await http.get(path)
            assert response.status_code == 200, response.text
            value = response.json()
            assert value['from']['revision'] == old and value['to']['revision'] == new
            chunks.append(value['diff'])
            path = value.get('next')
            assert len(chunks) < 10
    expected = ''.join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f'{canonical_path}@{old}',
            tofile=f'{canonical_path}@{new}',
        )
    )
    assert ''.join(chunks) == expected
