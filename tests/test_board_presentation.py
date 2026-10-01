import httpx
import pytest
from test_service import call, register

from msg.admin.boards import appoint_administrator
from msg.core.board_art import DEFAULTS, board_svg
from msg.core.codec import b64, wire
from msg.core.errors import Failure
from msg.core.profile_art import MAX_SVG_BYTES, safe_svg
from msg.security.crypto import Ed25519Signer
from msg.transports.http import create_app


def test_every_channel_has_distinct_safe_transparent_looping_art():
    fingerprints = set()
    for name, (description, _) in DEFAULTS.items():
        svg = board_svg('t_' + name, name)
        assert description and len(svg.encode()) < MAX_SVG_BYTES
        assert safe_svg(svg.encode()) and '<rect' not in svg
        assert 'infinite' in svg and 'prefers-reduced-motion' in svg
        assert svg == board_svg('t_' + name, name)
        fingerprints.add(svg)
    assert len(fingerprints) == len(DEFAULTS)


@pytest.mark.asyncio
async def test_root_appointments_board_files_and_small_projections(installed):
    app, root = installed
    first_key, first, _ = await register(app, 'board-editor')
    second_key, second, _ = await register(app, 'board-replacement')
    with pytest.raises(Failure, match='root_key_mismatch'):
        await appoint_administrator(
            app, '/wiki', '/@board-editor', Ed25519Signer.generate(), operator='test'
        )
    await appoint_administrator(app, '/wiki', '/@board-editor', root, operator='test')
    created = await call(
        app,
        'content.file_put',
        {
            'parent': '/wiki',
            'name': 'ABOUT.md',
            'media_type': 'text/markdown',
            'data': b64('A shared library / 共同知识库'.encode()),
        },
        key=first_key,
        subject=first,
    )
    assert created.status == 'ok', wire(created)
    denied = await call(
        app,
        'content.file_put',
        {
            'parent': '/wiki',
            'name': 'HEADER.svg',
            'media_type': 'image/svg+xml',
            'data': b64(board_svg('custom', 'wiki').encode()),
        },
        key=second_key,
        subject=second,
    )
    assert denied.error.code == 'topic_admin_required', wire(denied)
    custom = await call(
        app,
        'content.file_put',
        {
            'parent': '/wiki',
            'name': 'HEADER.svg',
            'media_type': 'image/svg+xml',
            'data': b64(board_svg('custom', 'wiki').encode()),
        },
        key=first_key,
        subject=first,
    )
    assert custom.status == 'ok', wire(custom)
    await appoint_administrator(
        app, '/wiki', '/@board-replacement', root, replace_admins=True, operator='test'
    )
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT role FROM topic_memberships WHERE topic=? AND subject=?', ('t_wiki', first)
            )[0]
            == 'member'
        )
        assert (
            tx.one(
                'SELECT role FROM topic_memberships WHERE topic=? AND subject=?', ('t_wiki', second)
            )[0]
            == 'admin'
        )
    denied_patch = await call(
        app,
        'content.text_patch',
        {
            'id': created.resources[0].id,
            'base_revision': created.resources[0].revision,
            'exact': 'A shared library',
            'replacement': 'Changed',
        },
        key=first_key,
        subject=first,
        expected=((created.resources[0].id, created.data['generation']),),
    )
    assert denied_patch.error.code == 'topic_admin_required', wire(denied_patch)
    changed = await call(
        app,
        'content.text_patch',
        {
            'id': custom.resources[0].id,
            'base_revision': custom.resources[0].revision,
            'exact': 'animated ASCII',
            'replacement': 'looping ASCII',
        },
        key=second_key,
        subject=second,
        expected=((custom.resources[0].id, custom.data['generation']),),
    )
    assert changed.status == 'ok', wire(changed)
    for channel in ('store', 'last-will'):
        await appoint_administrator(
            app, '/' + channel, '/@board-replacement', root, operator='test'
        )
        banner = await call(
            app,
            'content.file_put',
            {
                'parent': '/' + channel,
                'name': 'HEADER.svg',
                'media_type': 'image/svg+xml',
                'data': b64(board_svg('custom-' + channel, channel).encode()),
            },
            key=second_key,
            subject=second,
        )
        assert banner.status == 'ok', wire(banner)
    unsafe = await call(
        app,
        'content.file_put',
        {
            'parent': '/main',
            'name': 'HEADER.svg',
            'media_type': 'image/svg+xml',
            'data': b64(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        },
        key=second_key,
        subject=second,
    )
    assert unsafe.error.code == 'topic_admin_required', wire(unsafe)
    await appoint_administrator(app, '/main', '/@board-replacement', root, operator='test')
    unsafe = await call(
        app,
        'content.file_put',
        {
            'parent': '/main',
            'name': 'HEADER.svg',
            'media_type': 'image/svg+xml',
            'data': b64(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        },
        key=second_key,
        subject=second,
    )
    assert unsafe.error.code == 'invalid_board_svg', wire(unsafe)
    read = await call(app, 'discovery.get', {'id': '/wiki'})
    assert read.status == 'ok', wire(read)
    p = read.data['presentation']
    assert p['description'] == 'A shared library / 共同知识库'
    assert 'u_root' in p['administrators'] and second in p['administrators']
    assert len(str(p)) < 1200 and '<svg' not in str(p)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        art = await http.get(p['header']['url'])
        assert art.status_code == 200 and art.headers['x-msg-artwork-source'] == 'custom'
        assert art.text == board_svg('custom', 'wiki').replace('animated ASCII', 'looping ASCII')
        still = await http.get(p['header']['url'] + '?still=1')
        assert still.status_code == 200 and '*{animation:none!important}' in still.text
        head = await http.head(p['header']['url'])
        assert head.status_code == 200 and not head.content
        cached = await http.get(p['header']['url'], headers={'if-none-match': art.headers['etag']})
        assert cached.status_code == 304
        page = await http.get('/wiki', headers={'accept': 'text/html'})
        assert 'class="board-heading"' in page.text and '共同知识库' in page.text
        assert 'class="board-frame"' not in page.text
        private = await http.get('/_board/t_admins/header.svg')
        assert private.status_code >= 400
