"""Profiles list authored posts through the same ACL as a direct read."""

from dataclasses import replace

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.codec import canonical
from msg.plugins.profile import profile_markdown
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_profile_activity_hides_unreadable_posts_and_ancestors(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'profile-author')
    other_key, other_uid, other_cert = await register(app, 'other-author')
    paths = {}
    identifiers = {}
    for title, mode, state in (
        ('A readable title', 0o644, 'active'),
        ('PRIVATE TITLE', 0o600, 'active'),
        ('ARCHIVED TITLE', 0o644, 'archived'),
    ):
        result = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': '# ' + title + '\n\nA short explanation.'},
            key=key,
            subject=uid,
            certs=(cert,),
        )
        assert result.status == 'ok', result.error
        rid = result.resources[0].id
        identifiers[title] = rid
        paths[title] = '/*' + rid.split('_')[-1]
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(rid)
            await tx.replace(
                replace(resource, mode=mode, state=state, generation=resource.generation + 1),
                resource.generation,
            )
    other = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# OTHER AUTHOR'},
        key=other_key,
        subject=other_uid,
        certs=(other_cert,),
    )
    assert other.status == 'ok', other.error
    # The signing actor and the current owner can differ from the post author.
    # Preserve Revision.author while changing these projection facts directly.
    async with app.metadata.transaction(write=True) as tx:
        rid = identifiers['A readable title']
        resource = await tx.resource(rid)
        tx.execute(
            'UPDATE resources SET owner=?,body=? WHERE id=?',
            (
                other_uid,
                canonical(replace(resource, created_by=other_uid, owner=other_uid)).decode(),
                rid,
            ),
            write=True,
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/@profile-author')
        assert response.status_code == 200, response.text
        assert response.text.startswith('# @profile-author\n')
        assert 'Account: registered' in response.text
        assert 'Joined: 2026-09-27 08:00 (Asia/Taipei)' in response.text
        assert 'Visible posts: 1' in response.text
        assert f'[A readable title]({paths["A readable title"]})' in response.text
        assert 'A short explanation.' in response.text
        assert '[certificates](/@profile-author/cert)' in response.text
        assert '[keys](/@profile-author/k)' in response.text
        assert 'PRIVATE TITLE' not in response.text and 'ARCHIVED TITLE' not in response.text
        assert 'OTHER AUTHOR' not in response.text and 'p_' not in response.text
        projection = await http.get('/@profile-author/json')
        assert projection.status_code == 200, projection.text
        assert projection.json()['profile']['post_count'] == 1
        head = await http.head('/@profile-author')
        assert head.status_code == 200 and head.content == b''
        async with app.metadata.transaction(write=True) as tx:
            parent = await tx.resource(await tx.resolve('/main'))
            await tx.replace(
                replace(parent, mode=0o700, generation=parent.generation + 1), parent.generation
            )
        hidden = await http.get('/@profile-author')
        assert 'Visible posts: 0' in hidden.text and 'No visible posts yet.' in hidden.text
        assert 'A readable title' not in hidden.text
        empty = await http.get('/@other-author')
        assert empty.status_code == 200
        assert 'Visible posts: 0' in empty.text


def test_profile_markdown_escapes_authored_text_and_paths():
    text = profile_markdown({
        'name': '@alice',
        'kind': 'registered',
        'created_at': '2026-09-27T00:00:00+00:00',
        'profile': {
            'post_count': 1,
            'latest_posts': [
                {
                    'title': 'Title [link] <tag> & ! *text*',
                    'excerpt': '`code` {x} | [other]',
                    'path': '/*abc',
                    'created_at': '2026-09-27T00:00:00+00:00',
                }
            ],
        },
        'items': [{'name': '[resource]', 'path': '/@alice/a(b) c'}],
    })
    assert 'Title \\[link\\] \\<tag\\> \\& \\! \\*text\\*' in text
    assert '\\`code\\` \\{x\\} \\| \\[other\\]' in text
    assert '[\\[resource\\]](/@alice/a%28b%29%20c)' in text


@pytest.mark.asyncio
async def test_profile_bio_social_links_counts_and_lists_respect_visibility(installed):
    from msg.core.codec import b64

    app, _ = installed
    key, alice, _ = await register(app, 'social-alice')
    bk, bob, _ = await register(app, 'social-bob')
    ck, carol, _ = await register(app, 'social-carol')
    result = await call(
        app,
        'content.file_put',
        {
            'parent': '/@social-alice',
            'name': 'BIO.md',
            'data': b64('你好，写开源工具。 <script>alert(1)</script>'.encode()),
            'media_type': 'text/markdown',
        },
        key=key,
        subject=alice,
    )
    assert result.status == 'ok', result.error
    bio = result.resources[0].id
    for actor_key, actor, target in ((key, alice, bob), (bk, bob, alice), (ck, carol, alice)):
        followed = await call(
            app, 'communication.follow', {'id': target}, key=actor_key, subject=actor
        )
        assert followed.status == 'ok', followed.error
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        profile = await http.get('/@social-alice')
        assert '你好，写开源工具。' in profile.text
        assert '关注 · 1' in profile.text and '粉丝 · 2' in profile.text
        assert '(/@social-alice/follows)' in profile.text
        html = await http.get('/@social-alice/followers?limit=1', headers={'Accept': 'text/html'})
        assert html.status_code == 200 and 'Next page' in html.text
        assert 'limit=1' in html.text
        assert 'format=raw' in html.text and ('Mutual' in html.text or 'social-carol' in html.text)
        raw = await http.get('/@social-alice/followers?format=raw', headers={'Accept': 'text/html'})
        assert raw.status_code == 200 and raw.headers['content-type'].startswith('text/plain')
        assert (
            'social-bob' in raw.text and 'social-carol' in raw.text and '<!doctype' not in raw.text
        )
        api = await http.get('/@social-alice/followers', headers={'Accept': 'application/json'})
        assert len(api.json()['items']) == 2
        async with app.metadata.transaction(write=True) as tx:
            for rid in (bio, carol):
                resource = await tx.resource(rid)
                await tx.replace(
                    replace(resource, mode=0o700, generation=resource.generation + 1),
                    resource.generation,
                )
        hidden = (await http.get('/@social-alice/json')).json()['profile']
        assert not hidden['bio'] and hidden['bio_path'] is None
        assert hidden['follower_count'] == 1 and hidden['following_count'] == 1
        listing = await http.get('/@social-alice/followers', headers={'Accept': 'text/html'})
        assert 'social-carol' not in listing.text


@pytest.mark.asyncio
async def test_old_browser_social_authorization_uses_public_read_without_expanding_ceiling(oauth):
    from msg.security.oauth import OAuthService

    app, key, subject, http = oauth
    bk, bob, _ = await register(app, 'old-browser-followed')
    made = await call(app, 'communication.follow', {'id': bob}, key=key, subject=subject)
    assert made.status == 'ok'
    cookie = await browser_login(oauth)
    async with app.metadata.transaction(write=True) as tx:
        _, cid, _ = await OAuthService(app).browser_credentials(tx, cookie)
        original = await tx.credential(cid)
        narrowed = replace(
            original,
            ceiling=tuple(
                replace(
                    g,
                    operations=g.operations
                    - {'communication.agent_following@1', 'communication.followers@1'},
                )
                for g in original.ceiling
            ),
        )
        await tx.save_credential(narrowed, (await tx.subject(subject)).auth_version)
    html = await http.get('/@oauth-owner/follows', headers={'Accept': 'text/html'})
    assert html.status_code == 200 and 'old-browser-followed' in html.text
    assert 'Showing public relationships' in html.text
    raw = await http.get('/@oauth-owner/follows?format=raw', headers={'Accept': 'text/html'})
    assert raw.status_code == 200 and raw.headers['content-type'].startswith('text/plain')
    api = await http.get('/@oauth-owner/follows', headers={'Accept': 'application/json'})
    assert api.status_code == 403 and api.json()['error']['code'] == 'credential_ceiling'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.credential(cid)).ceiling == narrowed.ceiling
        resource = await tx.resource(bob)
    async with app.metadata.transaction(write=True) as tx:
        await tx.replace(
            replace(resource, mode=0o700, generation=resource.generation + 1), resource.generation
        )
    hidden = await http.get('/@oauth-owner/follows', headers={'Accept': 'text/html'})
    assert hidden.status_code == 200 and 'old-browser-followed' not in hidden.text
