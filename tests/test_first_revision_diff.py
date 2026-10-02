"""A first revision is a normal default view, never a forged comparison."""

from dataclasses import replace
from uuid import uuid4

import httpx
from test_linkset_diff import business_state
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.identifiers import hex_id
from msg.transports.http import create_app


async def test_first_revision_default_diff_preserves_explicit_ranges_and_html(installed):
    app, _ = installed
    key, user, _ = await register(app, 'first-diff')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '<script>before</script>\n'},
        key=key,
        subject=user,
    )
    ref = created.resources[0]
    other = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'other resource'},
        key=key,
        subject=user,
    )
    before = await business_state(app)
    for signer, subject in ((None, None), (key, user)):
        empty = await call(
            app,
            'discovery.diff_view',
            {'id': ref.id, 'previous': True},
            key=signer,
            subject=subject,
        )
        assert empty.status == 'ok', empty.error
        assert empty.data == {
            'from': None,
            'to': {'id': ref.id, 'revision': ref.revision},
            'diff': '',
            'reason': 'no_previous_revision',
        }
        for missing in ('v_missing', other.resources[0].revision):
            explicit = await call(
                app,
                'discovery.diff_view',
                {'id': ref.id, 'known_revision': missing},
                key=signer,
                subject=subject,
            )
            assert explicit.error.code == 'revision_not_found'
        invalid = await call(
            app,
            'discovery.diff_view',
            {'id': ref.id, 'previous': True, 'known_revision': ref.revision},
            key=signer,
            subject=subject,
        )
        assert invalid.error.code == 'invalid_diff_range'
    path = '/*' + hex_id(ref.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        json = await http.get(path + '/diff')
        assert json.status_code == 200 and json.json()['reason'] == 'no_previous_revision'
        for method in ('get', 'head'):
            response = await getattr(http, method)(path + '/diff', headers={'Accept': 'text/html'})
            assert response.status_code == 200
            assert response.headers['content-type'].startswith('text/html')
            if method == 'get':
                assert (
                    'This is the first version' in response.text and '这是第一版' in response.text
                )
                assert '<script>before</script>' not in response.text
                assert 'No text changes' not in response.text
            else:
                assert response.content == b''
        missing = await http.get(path + '/diff/v_missing', headers={'Accept': 'text/html'})
        assert (
            missing.status_code == 404 and missing.json()['error']['code'] == 'revision_not_found'
        )
        same = await http.get(
            path + '/diff/' + hex_id(ref.revision), headers={'Accept': 'text/html'}
        )
        assert same.status_code == 200 and 'No text changes' in same.text
        assert 'This is the first version' not in same.text
    assert await business_state(app) == before
    edited = await call(
        app,
        'content.post_edit',
        {'id': ref.id, 'expected_revision': ref.revision, 'body': '<script>after</script>\n'},
        key=key,
        subject=user,
        expected=((ref.id, created.data['generation']),),
    )
    assert edited.status == 'ok', edited.error
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get(path + '/diff', headers={'Accept': 'text/html'})
        assert response.status_code == 200 and response.headers['content-type'].startswith(
            'text/html'
        )
        assert '&lt;script&gt;before&lt;/script&gt;' in response.text
        assert '&lt;script&gt;after&lt;/script&gt;' in response.text
        assert (
            '<script>after</script>' not in response.text
            and 'This is the first version' not in response.text
        )
    async with app.metadata.transaction(write=True) as tx:
        current = await tx.revision(edited.resources[0])
        merged = replace(current, id='v_' + uuid4().hex, parents=(ref.revision, current.id))
        await tx.append_revision(merged)
        resource = await tx.resource(ref.id)
        await tx.replace(
            replace(resource, revision=merged.id, generation=resource.generation + 1),
            resource.generation,
        )
    merge = await call(app, 'discovery.diff_view', {'id': ref.id, 'previous': True})
    assert merge.error.code == 'previous_revision_not_found'


async def test_browser_first_revision_diff_rechecks_current_private_authority(oauth):
    app, key, user, http = oauth
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'private first revision'},
        key=key,
        subject=user,
    )
    ref = created.resources[0]
    locked = await call(
        app,
        'content.chmod',
        {'id': ref.id, 'mode': '0600'},
        key=key,
        subject=user,
        expected=((ref.id, created.data['generation']),),
    )
    assert locked.status == 'ok', locked.error
    path = '/*' + hex_id(ref.id) + '/diff'
    denied = await http.get(path, headers={'Accept': 'text/html'})
    assert denied.status_code == 403 and 'no_previous_revision' not in denied.text
    assert ref.revision not in denied.text and 'private first revision' not in denied.text
    await browser_login(oauth)
    before = await business_state(app)
    allowed = await http.get(path, headers={'Accept': 'text/html'})
    assert allowed.status_code == 200 and 'This is the first version' in allowed.text
    assert allowed.headers['content-type'].startswith('text/html')
    assert await business_state(app) == before
    http.cookies.clear()
    revoked = await http.get(path, headers={'Accept': 'text/html'})
    assert revoked.status_code == 403 and 'This is the first version' not in revoked.text
    assert 'private first revision' not in revoked.text


async def test_first_wiki_revision_has_history_without_invalid_diff_button(installed):
    app, _ = installed
    key, user, _ = await register(app, 'first-wiki-diff')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/wiki', 'body': 'first wiki content'},
        key=key,
        subject=user,
    )
    ref = created.resources[0]
    path = '/*' + hex_id(ref.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        page = await http.get(path, headers={'Accept': 'text/html'})
        assert page.status_code == 200 and f'href="{path}/history"' in page.text
        assert f'href="{path}/diff"' not in page.text
        empty = await http.get(path + '/diff', headers={'Accept': 'text/html'})
        assert empty.status_code == 200 and 'This is the first version' in empty.text
