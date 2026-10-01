"""Profiles list authored posts through the same ACL as a direct read."""

from dataclasses import replace

import httpx
import pytest
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
