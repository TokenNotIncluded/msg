"""Profile media must remain bounded, inert and subject to ordinary read access."""

from dataclasses import replace
from xml.etree import ElementTree as ET

import httpx
import pytest
from test_service import call, register

from msg.core.codec import b64
from msg.core.profile_art import MAX_SVG_BYTES, generate_svg, safe_svg, still_svg
from msg.transports.home_page import document_html
from msg.transports.http import create_app


def test_random_art_is_reproducible_distinct_bounded_and_looping():
    for kind in ('avatar', 'background'):
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
        assert data['profile']['artwork']['avatar'] == {'svg': svg, 'source': 'custom'}
        assert data['profile']['artwork']['background']['source'] == 'generated'
        html = await http.get('/@svg-author', headers={'Accept': 'text/html'})
        assert html.status_code == 200 and 'profile-avatar' in html.text
        assert 'profile-background' in html.text and 'Pause animation' in html.text
        assert "img-src 'self' data:" in html.headers['content-security-policy']
        raw = await http.get('/@svg-author?format=raw', headers={'Accept': 'text/html'})
        assert 'data:image/svg' not in raw.text
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(made.resources[0].id)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        hidden = (await http.get('/@svg-author/json')).json()
        assert hidden['profile']['artwork']['avatar']['source'] == 'generated'
        assert 'custom artwork' not in str(hidden['profile']['artwork'])


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
    assert 'IntersectionObserver' in html and 'visibilitychange' in html
