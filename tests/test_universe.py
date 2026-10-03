"""Universe is a public read projection, never a second identity/ACL system."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from read_only_evidence import readonly_evidence
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.bootstrap import ROOT_WEB_SAMPLE
from msg.core.codec import digest
from msg.extensions.hosting import HOSTED_HEADERS, hosted_headers
from msg.security.oauth import get, state_id
from msg.transports.http import create_app
from msg.transports.oauth_http import csrf


@pytest.mark.asyncio
async def test_universe_account_hint_is_minimal_current_identity_without_private_reads(
    oauth, monkeypatch
):
    app, key, subject, http = oauth
    _, other, _ = await register(app, 'hint-private-contact')
    private = await call(
        app,
        'communication.dm_request',
        {'recipient': other, 'introduction': 'ACCOUNT HINT MUST NOT LOAD THIS DM'},
        key=key,
        subject=subject,
        contract_version=2,
    )
    assert private.status == 'ok', private.error
    assert (await http.get('/_universe/account')).json() == {'version': 1, 'account': None}
    await browser_login(oauth)
    async with app.metadata.transaction(write=True) as tx:
        own = await tx.resource(subject)
        await tx.replace(replace(own, mode=0o600, generation=own.generation + 1), own.generation)
    requests = []
    execute = app.executor.execute

    async def tracked(request, **kwargs):
        requests.append((request.operation, dict(request.arguments)))
        return await execute(request, **kwargs)

    monkeypatch.setattr(app.executor, 'execute', tracked)
    async with readonly_evidence(app, monkeypatch):
        response = await http.get('/_universe/account')
        assert response.status_code == 200, response.text
        assert response.json() == {
            'version': 1,
            'account': {'id': subject, 'name': '@oauth-owner'},
        }
        assert response.headers['cache-control'] == 'private, no-store'
        assert {value.strip() for value in response.headers['vary'].split(',')} == {'Cookie'}
        assert requests == [('discovery.get', {'id': subject, 'fields': ('id', 'name')})]
        assert private.data['conversation_id'] not in response.text
        assert 'ACCOUNT HINT MUST NOT LOAD THIS DM' not in response.text
        assert (await http.head('/_universe/account')).content == b''
        for query in ('subject=' + other, 'fields=star_private', 'kind=users', 'x=1&x=2'):
            assert (await http.get('/_universe/account?' + query)).status_code == 400
        assert (await http.post('/_universe/account')).status_code == 405
        assert (
            await http.get('/_universe/account', headers={'Authorization': 'Bearer inert'})
        ).status_code == 400
        assert (
            await http.get('/_universe/account', headers={'x-msg-request': 'inert'})
        ).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['expired', 'logout', 'source_revoked'])
async def test_universe_account_hint_clears_expired_or_revoked_browser_identity(oauth, boundary):
    app, key, subject, http = oauth
    cookie = await browser_login(oauth)
    assert (await http.get('/_universe/account')).json()['account']['id'] == subject
    if boundary == 'expired':
        async with app.metadata.transaction(write=False) as tx:
            expires, _ = get(tx, state_id('session', cookie), app.clock())
        app._oauth_clock[0] = expires + timedelta(seconds=1)
    elif boundary == 'logout':
        result = await http.post(
            '/oauth/logout',
            json={'csrf': csrf(cookie)},
            headers={'Origin': app.settings.service_url},
        )
        assert result.status_code == 200 and result.json() == {'logged_out': True}
        http.cookies.set('msg_session', cookie)
    else:
        async with app.metadata.transaction(write=True) as tx:
            credential = await tx.credential(key.key_id)
            owner = await tx.subject(subject)
            await tx.save_credential(
                replace(credential, revoked_at=app.clock()), owner.auth_version
            )
    response = await http.get('/_universe/account')
    assert response.status_code == 200, response.text
    assert response.json() == {'version': 1, 'account': None}
    assert response.headers['cache-control'] == 'private, no-store'


@pytest.mark.asyncio
async def test_universe_avatar_reference_uses_current_anonymous_access(oauth, monkeypatch):
    from msg.core.codec import b64

    app, key, subject, signed = oauth
    _, hidden_subject, _ = await register(app, 'hidden-avatar-star')
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><text>PUBLIC AVATAR</text>'
        '<animate attributeName="opacity" values="1;0.5;1" dur="2s" '
        'repeatCount="indefinite"/></svg>'
    )
    made = await call(
        app,
        'content.file_put',
        {
            'parent': '/@oauth-owner',
            'name': 'AVATAR.svg',
            'data': b64(svg.encode()),
            'media_type': 'image/svg+xml',
        },
        key=key,
        subject=subject,
    )
    assert made.status == 'ok', made.error
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(hidden_subject)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    await browser_login(oauth)

    async def no_full_profile(*args, **kwargs):
        pytest.fail('a universe avatar reference must not scan full profile activity')

    monkeypatch.setattr('msg.plugins.profile.account_activity', no_full_profile)
    expected = {'avatar': {'url': '/@oauth-owner/art/avatar.svg'}}
    for query in ('kind=users', 'kind=users&shuffle=1'):
        response = await signed.get('/_universe?' + query)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['anchor']['artwork'] == {'avatar': {'url': '/@root/art/avatar.svg'}}
        for item in data['items']:
            assert item['artwork'] == {'avatar': {'url': item['path'] + '/art/avatar.svg'}}
            assert 'profile' not in item
        assert hidden_subject not in response.text
        assert made.resources[0].id not in response.text
        assert made.resources[0].revision not in response.text
        assert 'AVATAR.svg' not in response.text and '<svg' not in response.text
    refreshed = await signed.get(
        '/_universe', params={'kind': 'users', 'ids': ','.join((subject, hidden_subject, 'u_root'))}
    )
    assert refreshed.status_code == 200, refreshed.text
    nodes = {item['id']: item for item in refreshed.json()['items']}
    assert set(nodes) == {subject, 'u_root'}
    assert nodes[subject]['artwork'] == expected
    assert nodes['u_root']['artwork'] == {'avatar': {'url': '/@root/art/avatar.svg'}}
    minimal = await call(app, 'discovery.get', {'id': subject, 'fields': ['artwork']})
    assert minimal.status == 'ok' and minimal.data == {'artwork': expected}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as anonymous:
        asset = await anonymous.get(expected['avatar']['url'])
        assert asset.status_code == 200 and asset.headers['x-msg-artwork-source'] == 'custom'
        assert 'PUBLIC AVATAR' in asset.text and '<animate' in asset.text
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(made.resources[0].id)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        # The logged-in owner still gets the same anonymous tiny projection;
        # fetching its public endpoint must recheck the now-private file ACL.
        private = await signed.get('/_universe', params={'kind': 'users', 'ids': subject})
        assert private.status_code == 200, private.text
        assert private.json()['items'][0]['artwork'] == expected
        assert made.resources[0].id not in private.text
        assert made.resources[0].revision not in private.text
        assert 'AVATAR.svg' not in private.text and 'PUBLIC AVATAR' not in private.text
        denied = await anonymous.get(
            expected['avatar']['url'], headers={'If-None-Match': asset.headers['etag']}
        )
        assert denied.status_code == 200
        assert denied.headers['x-msg-artwork-source'] == 'generated'
        assert 'PUBLIC AVATAR' not in denied.text
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(subject)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        hidden = await signed.get('/_universe', params={'kind': 'users', 'ids': subject})
        assert hidden.status_code == 200 and not hidden.json()['items']
        assert '/@oauth-owner' not in hidden.text
        assert (await anonymous.get(expected['avatar']['url'])).status_code == 403


@pytest.mark.asyncio
async def test_public_universe_excludes_private_posts_and_private_ancestors(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'star-author')
    public = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# Visible star'},
        key=key,
        subject=subject,
    )
    secret = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# SECRET STAR'},
        key=key,
        subject=subject,
    )
    assert public.status == secret.status == 'ok'
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(secret.resources[0].id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        stars = await http.get('/_universe?kind=users')
        assert stars.status_code == 200, stars.text
        assert subject in [item['id'] for item in stars.json()['items']]
        posts = await http.get('/_universe?kind=posts')
        assert posts.status_code == 200, posts.text
        assert 'Visible star' in posts.text and 'SECRET STAR' not in posts.text
        assert secret.resources[0].id not in posts.text
        assert posts.json()['items'][0]['author']['id'] == subject
        assert posts.headers['cache-control'] == 'private, no-store'
        head = await http.head('/_universe?kind=posts')
        assert head.status_code == 200 and not head.content
        for query in (
            'kind=users&kind=posts',
            'kind=secret',
            'kind=posts&limit=999999',
            'kind=posts&cursor=invalid',
        ):
            assert (await http.get('/_universe?' + query)).status_code == 400
        assert (await http.post('/_universe')).status_code == 405
        assert (await http.get('/_universe/me?subject=' + subject)).status_code == 400
        async with app.metadata.transaction(write=True) as tx:
            topic = await tx.resource(await tx.resolve('/main'))
            await tx.replace(
                replace(topic, mode=0o700, generation=topic.generation + 1), topic.generation
            )
        assert not (await http.get('/_universe?kind=posts')).json()['items']


@pytest.mark.asyncio
async def test_universe_private_view_uses_only_current_browser_identity(oauth):
    app, key, subject, http = oauth
    other_key, other, _ = await register(app, 'other-star')
    request = await call(
        app,
        'communication.dm_request',
        {'recipient': other, 'introduction': 'PRIVATE INTRO'},
        key=key,
        subject=subject,
        contract_version=2,
    )
    assert request.status == 'ok', request.error
    rid = request.data['conversation_id']
    anonymous = await http.get('/_universe/me')
    assert anonymous.json()['account'] is None
    assert rid not in anonymous.text
    await browser_login(oauth)
    private = await http.get('/_universe/me')
    assert private.status_code == 200, private.text
    assert private.json()['account']['id'] == subject
    assert private.json()['conversations'][0]['conversation_id'] == rid
    public = await http.get('/_universe?kind=posts')
    assert rid not in public.text and 'PRIVATE INTRO' not in public.text
    assert (await http.get('/_universe/me?subject=' + other)).status_code == 400
    assert 'no-store' in private.headers['cache-control']
    http.cookies.clear()
    assert (await http.get('/_universe/me')).json()['conversations'] == []


def test_only_exact_release_gets_trusted_universe_origin():
    headers = hosted_headers('w_root_web', 'index.html', digest(ROOT_WEB_SAMPLE))
    csp = headers['Content-Security-Policy']
    assert 'sandbox allow-scripts allow-same-origin' in csp
    assert "connect-src 'self'" in csp
    assert "script-src 'sha256-" in csp and "'unsafe-eval'" not in csp
    for site, path, body in [
        ('other', 'index.html', ROOT_WEB_SAMPLE),
        ('w_root_web', 'different.html', ROOT_WEB_SAMPLE),
        ('w_root_web', 'index.html', ROOT_WEB_SAMPLE + b'changed'),
    ]:
        assert hosted_headers(site, path, digest(body)) == HOSTED_HEADERS
    assert b'__UNIVERSE_' not in ROOT_WEB_SAMPLE
    assert b'canvas' in ROOT_WEB_SAMPLE and b'constellation' in ROOT_WEB_SAMPLE


@pytest.mark.asyncio
async def test_browser_signature_interoperates_with_real_executor(installed):
    import json
    import subprocess
    from importlib.resources import files

    from test_service import NOW

    from msg.core.codec import b64, decode
    from msg.core.models import OperationRequest

    app, _ = installed
    key, subject, _ = await register(app, 'browser-signer')
    program = """
    const fs = require('node:fs');
    const input = JSON.parse(fs.readFileSync(0, 'utf8'));
    const OriginalDate = Date;
    globalThis.Date = class extends OriginalDate {
      constructor(...args) { super(...(args.length ? args : [input.now])); }
      static now() { return new OriginalDate(input.now).getTime(); }
    };
    eval(input.model);
    (async () => {
      const seed = Uint8Array.from(Buffer.from(input.seed, 'base64url'));
      const signer = await MSGUniverse.importSigner(seed, input.identity);
      if (seed.some(Boolean) || signer.key.extractable) throw new Error('key handling');
      const packet = await MSGUniverse.packet(signer, input.service,
        'content.post_create', {parent:'/main', body:'A star: 宇宙 🌟'}, 'browser-proof');
      console.log(JSON.stringify(packet));
    })().catch(error => { console.error(error.message); process.exitCode = 1; });
    """
    completed = subprocess.run(
        ['node', '-e', program],
        input=json.dumps({
            'model': files('msg.data').joinpath('root-web-model.js').read_text(),
            'now': NOW.isoformat(),
            'seed': b64(key.private_bytes()),
            'service': app.settings.service_url,
            'identity': {
                'subject_id': subject,
                'key_id': key.key_id,
                'public_key': b64(key.public_key),
                'retired_at': None,
            },
        }),
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    packet = decode(OperationRequest, json.loads(completed.stdout))
    result = await app.executor.execute(packet)
    assert result.status == 'ok', result.error
    repeated = await app.executor.execute(packet)
    assert repeated.status == 'ok' and repeated.replayed
    read = await call(app, 'discovery.get', {'id': result.resources[0].id})
    assert read.data['content'] == 'A star: 宇宙 🌟'


@pytest.mark.asyncio
async def test_random_sector_is_a_bounded_anonymous_seek(installed, monkeypatch):
    app, _ = installed
    ids = sorted([(await register(app, 'shuffle-' + str(i)))[1] for i in range(4)])
    # Seek after one real hashed identity, including a hidden identity in the
    # same range. The anonymous reader must still exclude it.
    hidden = ids[2]
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(hidden)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    monkeypatch.setattr('msg.transports.universe.secrets.token_hex', lambda n: ids[1][2:])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/_universe?kind=users&shuffle=1')
        assert response.status_code == 200, response.text
        page = response.json()
        visible = {item['id'] for item in page['items']}
        assert ids[3] in visible and ids[0] not in visible and ids[1] not in visible
        assert hidden not in visible and len(visible) <= 100
        assert page['anchor']['id'] == 'u_root'
        assert response.headers['cache-control'] == 'private, no-store'
        for query in (
            'kind=posts&shuffle=1',
            'kind=users&shuffle=0',
            'kind=users&shuffle=1&cursor=bad',
            'kind=users&shuffle=1&ids=' + ids[0],
            'kind=users&shuffle=1&author=' + ids[0],
        ):
            assert (await http.get('/_universe?' + query)).status_code == 400
        monkeypatch.setattr('msg.transports.universe.secrets.token_hex', lambda n: 'f' * 32)
        wrapped = await http.get('/_universe?kind=users&shuffle=1')
        assert wrapped.status_code == 200, wrapped.text
        assert ids[0] in {item['id'] for item in wrapped.json()['items']}
        assert hidden not in {item['id'] for item in wrapped.json()['items']}


@pytest.mark.asyncio
async def test_universe_cursor_is_bound_and_reply_targets_are_filtered(installed):
    from msg.core.codec import canonical

    app, _ = installed
    key, subject, _ = await register(app, 'reply-star')
    target = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'private parent'},
        key=key,
        subject=subject,
    )
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': target.resources[0].id}, 'body': 'public reply'},
        key=key,
        subject=subject,
    )
    assert reply.status == 'ok', reply.error
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(target.resources[0].id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    for index in range(25):
        created = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': 'Signal ' + str(index)},
            key=key,
            subject=subject,
        )
        assert created.status == 'ok', created.error
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        first = (await http.get('/_universe?kind=posts')).json()
        assert len(first['items']) == 24 and first['cursor']
        cursor = first['cursor']
        rejected = await http.get('/_universe', params={'kind': 'users', 'cursor': cursor})
        assert rejected.status_code == 400
        second = (await http.get('/_universe', params={'kind': 'posts', 'cursor': cursor})).json()
        items = first['items'] + second['items']
        assert len({item['id'] for item in items}) == len(items) == 26
        visible_reply = next(item for item in items if item['id'] == reply.resources[0].id)
        assert visible_reply['reply_to'] is None
        assert target.resources[0].id.encode() not in canonical(items)
