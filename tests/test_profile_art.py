"""Profile media must remain bounded, inert and subject to ordinary read access."""

from dataclasses import replace
from xml.etree import ElementTree as ET

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.codec import b64
from msg.core.profile_art import MAX_SVG_BYTES, generate_svg, safe_svg, still_svg
from msg.transports.home_page import document_html
from msg.transports.http import create_app


def test_random_art_is_reproducible_distinct_bounded_and_looping():
    for kind in ('avatar', 'background', 'footer'):
        for seed in range(12):
            svg = generate_svg(seed, kind)
            assert len(svg.encode()) < MAX_SVG_BYTES
            assert safe_svg(svg.encode()) == svg
            assert 'infinite' in svg and 'prefers-reduced-motion' in svg
            assert generate_svg(seed, kind) == svg
            assert generate_svg(seed + 1, kind) != svg
            ET.fromstring(still_svg(svg))


@pytest.mark.parametrize(
    'payload',
    [
        '<script>alert(1)</script>',
        '<foreignObject><div>active HTML</div></foreignObject>',
        '<image href="https://example.com/tracker"/>',
        '<text onclick="alert(1)">x</text>',
        '<style>text{fill:url(https://example.com)}</style>',
        '<animate attributeName="href" values="javascript:alert(1)"/>',
        '<animate attributeName="onload" values="alert(1)"/>',
    ],
)
def test_svg_rejects_executable_and_external_content(payload):
    assert (
        safe_svg(('<svg xmlns="http://www.w3.org/2000/svg">' + payload + '</svg>').encode()) is None
    )


@pytest.mark.asyncio
async def test_custom_profile_images_obey_acl_and_invalid_svg_falls_back(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'svg-author')
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>custom artwork</text></svg>'
    made = await call(
        app,
        'content.file_put',
        {
            'parent': '/@svg-author',
            'name': 'AVATAR.svg',
            'data': b64(svg.encode()),
            'media_type': 'image/svg+xml',
        },
        key=key,
        subject=uid,
    )
    assert made.status == 'ok', made.error
    footer = await call(
        app,
        'content.file_put',
        {
            'parent': '/@svg-author',
            'name': 'FOOTER.svg',
            'data': b64(svg.replace('custom artwork', 'custom ocean').encode()),
            'media_type': 'image/svg+xml',
        },
        key=key,
        subject=uid,
    )
    assert footer.status == 'ok', footer.error
    invalid = await call(
        app,
        'content.file_put',
        {
            'parent': '/@svg-author',
            'name': 'BACKGROUND.svg',
            'data': b64(b'<svg><script>bad</script></svg>'),
            'media_type': 'image/svg+xml',
        },
        key=key,
        subject=uid,
    )
    assert invalid.status == 'ok', invalid.error
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        data = (await http.get('/@svg-author/json')).json()
        assert data['profile']['artwork']['avatar'] == {
            'url': '/@svg-author/art/avatar.svg',
            'file': '/@svg-author/AVATAR.svg',
        }
        assert '<svg' not in str(data) and 'data:image' not in str(data)
        assert len(str(data['profile']['artwork'])) < 512
        asset = await http.get('/@svg-author/art/background.svg')
        assert asset.headers['x-msg-artwork-source'] == 'generated'
        asset = await http.get('/@svg-author/art/footer.svg')
        assert asset.headers['x-msg-artwork-source'] == 'custom'
        assert 'custom ocean' in asset.text
        assert 'style-src' in asset.headers['content-security-policy']
        still = await http.get('/@svg-author/art/footer.svg?still=1')
        assert still.status_code == 200 and 'animation:none' in still.text
        head = await http.head('/@svg-author/art/footer.svg')
        assert head.status_code == 200 and head.content == b''
        cached = await http.get(
            '/@svg-author/art/footer.svg', headers={'If-None-Match': asset.headers['etag']}
        )
        assert cached.status_code == 304
        html = await http.get('/@svg-author', headers={'Accept': 'text/html'})
        assert html.status_code == 200 and 'profile-avatar' in html.text
        assert 'profile-background' in html.text
        assert 'Pause animation' not in html.text and 'profile-motion' not in html.text
        assert '<footer class="profile-ocean"' in html.text
        assert html.text.count('data-motion-src=""') == 3
        assert "img-src 'self' data:" in html.headers['content-security-policy']
        raw = await http.get('/@svg-author?format=raw', headers={'Accept': 'text/html'})
        assert 'data:image/svg' not in raw.text
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(made.resources[0].id)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(footer.resources[0].id)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        hidden = (await http.get('/@svg-author/json')).json()
        assert hidden['profile']['artwork']['avatar']['file'] is None
        assert 'custom artwork' not in str(hidden['profile']['artwork'])
        assert hidden['profile']['artwork']['footer']['file'] is None
        denied = await http.get(
            '/@svg-author/art/footer.svg', headers={'If-None-Match': asset.headers['etag']}
        )
        assert denied.status_code == 200
        assert denied.headers['x-msg-artwork-source'] == 'generated'
        assert 'custom ocean' not in denied.text
        assert 'custom ocean' not in str(hidden['profile']['artwork'])


def test_browser_artwork_escapes_names_and_uses_isolated_images():
    value = {
        'id': 'u_demo',
        'name': '@<script>',
        'type': 'user',
        'created_at': '',
        'profile': {'post_count': 0, 'latest_posts': [], 'bio': '<script>bad</script>'},
    }
    html = document_html('', resource=value).decode()
    assert '@&lt;script&gt;' in html and '<script>bad</script>' not in html
    assert '<img class="profile-avatar"' in html
    assert 'src="data:image' not in html
    assert 'Looping ASCII' not in html and '@keyframes tide' not in html
    assert '/art/footer.svg' in html
    assert 'IntersectionObserver' in html and 'visibilitychange' in html


@pytest.mark.asyncio
async def test_default_system_art_uses_authoritative_roles_and_is_stable(installed):
    app, _ = installed
    _, uid, _ = await register(app, 'role-art-reader')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for kind in ('avatar', 'background'):
            root = await http.get('/@root/art/' + kind + '.svg')
            ca = await http.get('/@online-ca/art/' + kind + '.svg')
            normal = await http.get('/@role-art-reader/art/' + kind + '.svg')
            assert root.status_code == ca.status_code == normal.status_code == 200
            assert root.text == generate_svg('u_root', kind, 'root')
            assert ca.text == generate_svg('u_online_ca', kind, 'online_ca')
            assert normal.text == generate_svg(uid, kind)
            assert len({root.text, ca.text, normal.text}) == 3
            repeated = await http.get('/@root/art/' + kind + '.svg')
            assert (
                repeated.content == root.content
                and repeated.headers['etag'] == root.headers['etag']
            )


@pytest.mark.asyncio
async def test_owner_can_create_and_replace_artwork_with_existing_signed_file_operations(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'art-owner')
    other_key, other, _ = await register(app, 'art-stranger')
    first = '<svg xmlns="http://www.w3.org/2000/svg"><text>owner first</text></svg>'
    second = first.replace('owner first', 'owner second')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        missing = await http.get(
            '/@art-owner/AVATAR.svg/meta', headers={'Accept': 'application/json'}
        )
        assert missing.status_code == 404
        args = {
            'parent': '/@art-owner',
            'name': 'AVATAR.svg',
            'data': b64(first.encode()),
            'media_type': 'image/svg+xml',
        }
        denied = await call(app, 'file.create', args, key=other_key, subject=other)
        assert denied.status == 'error'
        created = await call(app, 'file.create', args, key=key, subject=uid)
        assert created.status == 'ok', created.error
        meta_response = await http.get(
            '/@art-owner/AVATAR.svg/meta', headers={'Accept': 'application/json'}
        )
        assert meta_response.status_code == 200
        meta = meta_response.json()
        args = {
            'id': meta['id'],
            'base_revision': meta['revision'],
            'data': b64(second.encode()),
            'media_type': 'image/svg+xml',
        }
        expected = ((meta['id'], meta['generation']),)
        denied = await call(
            app, 'file.write', args, key=other_key, subject=other, expected=expected
        )
        assert denied.status == 'error'
        untouched = await http.get('/@art-owner/art/avatar.svg')
        assert untouched.text == first and untouched.headers['x-msg-artwork-source'] == 'custom'
        updated = await call(
            app,
            'file.write',
            args,
            key=key,
            subject=uid,
            expected=expected,
            rid='profile-art-update',
        )
        assert updated.status == 'ok', updated.error
        retried = await call(
            app,
            'file.write',
            args,
            key=key,
            subject=uid,
            expected=expected,
            rid='profile-art-update',
        )
        assert retried.status == 'ok' and retried.resources == updated.resources
        visible = await http.get('/@art-owner/art/avatar.svg')
        assert visible.text == second and visible.headers['x-msg-artwork-source'] == 'custom'
        stale = await call(app, 'file.write', args, key=key, subject=uid, expected=expected)
        assert stale.status == 'error'
        ordinary_write = await http.post('/@art-owner/AVATAR.svg', content=first)
        assert ordinary_write.status_code == 405


@pytest.mark.asyncio
async def test_artwork_editor_does_not_expand_browser_file_write_authority(oauth):
    from msg.security.oauth import OAuthService

    app, _, subject, _ = oauth
    cookie = await browser_login(oauth)
    async with app.metadata.transaction(write=False) as tx:
        _, credential_id, token = await OAuthService(app).browser_credentials(tx, cookie)
        credential = await tx.credential(credential_id)
        operations = {operation for grant in credential.ceiling for operation in grant.operations}
    assert not operations & {'file.create@1', 'file.write@1', 'content.file_put@1'}
    denied = await call(
        app,
        'file.create',
        {
            'parent': '/@oauth-owner',
            'name': 'AVATAR.svg',
            'data': b64(b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
            'media_type': 'image/svg+xml',
        },
        subject=subject,
        token=token,
    )
    assert denied.status == 'error'
