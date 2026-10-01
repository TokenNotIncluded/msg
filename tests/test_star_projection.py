"""Visual facts use current authority; they are not a second permission model."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import NOW, call, register

from msg.admin.money import apply_money
from msg.constants import ROOT_SUBJECT
from msg.core.codec import wire
from msg.transports.http import create_app


async def star(app, subject):
    result = await call(app, 'discovery.get', {'id': subject, 'fields': ['id', 'star']})
    assert result.status == 'ok', wire(result)
    return result.data['star']


@pytest.mark.asyncio
async def test_star_projection_is_explicit_and_anonymous_only(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'field-user')
    ordinary = await call(app, 'discovery.get', {'id': subject})
    assert ordinary.status == 'ok' and 'star' not in ordinary.data
    facts = await star(app, subject)
    assert facts['role'] == 'user'
    assert facts['certificate']['state'] == 'valid'
    assert facts['presence'] == {'state': 'unknown'}
    assert facts['last_public_post_at'] is None
    assert facts['balance'] == {'visibility': 'private'}
    signed = await call(
        app, 'discovery.get', {'id': subject, 'fields': ['star']}, key=key, subject=subject
    )
    assert signed.status == 'error' and signed.error.code == 'public_star_summary_only'


@pytest.mark.asyncio
@pytest.mark.parametrize('broken', ['leaf', 'issuer', 'key', 'expiry', 'hidden'])
async def test_certificate_badge_rechecks_chain_key_time_and_readability(
    installed, monkeypatch, broken
):
    app, _ = installed
    key, subject, cid = await register(app, 'field-certificate')
    assert (await star(app, subject))['certificate']['state'] == 'valid'
    async with app.metadata.transaction(write=True) as tx:
        cert = await tx.certificate(cid)
        if broken in {'leaf', 'issuer'}:
            revoked = cid if broken == 'leaf' else cert.parent_certificate_id
            tx.execute('UPDATE certificates SET revoked=1 WHERE id=?', (revoked,), write=True)
        elif broken == 'key':
            credential = await tx.credential(key.key_id)
            identity = await tx.subject(subject)
            await tx.save_credential(replace(credential, revoked_at=NOW), identity.auth_version)
        elif broken == 'hidden':
            resource = await tx.resource(cid)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
    if broken == 'expiry':
        monkeypatch.setattr(app.certificates, 'clock', lambda: cert.expires_at)
    facts = await star(app, subject)
    assert facts['certificate']['state'] != 'valid'
    assert 'id' not in facts['certificate'] and 'path' not in facts['certificate']


@pytest.mark.asyncio
async def test_star_presence_uses_existing_ttl_without_status_text(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'field-presence')
    set_result = await call(
        app,
        'communication.presence_set',
        {
            'state': 'available',
            'ttl': 30,
            'message': 'private-working-note',
            'capabilities_hint': ['implementation-detail'],
        },
        key=key,
        subject=subject,
    )
    assert set_result.status == 'ok', wire(set_result)
    facts = await star(app, subject)
    assert facts['presence']['state'] == 'available'
    assert facts['presence']['self_reported'] is True
    assert 'private-working-note' not in str(facts)
    assert 'implementation-detail' not in str(facts)
    app.executor.clock = lambda: NOW + timedelta(seconds=31)
    assert (await star(app, subject))['presence'] == {'state': 'unknown'}


@pytest.mark.asyncio
async def test_private_posts_and_hidden_ancestors_do_not_light_public_stars(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'field-activity')
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'Private activity'},
        key=key,
        subject=subject,
    )
    assert post.status == 'ok', wire(post)
    assert (await star(app, subject))['last_public_post_at'] == wire(NOW)
    async with app.metadata.transaction(write=True) as tx:
        topic = await tx.resource(await tx.resolve('/main'))
        await tx.replace(
            replace(topic, mode=0o700, generation=topic.generation + 1), topic.generation
        )
    facts = await star(app, subject)
    assert facts['last_public_post_at'] is None
    assert facts['presence'] == {'state': 'unknown'}


@pytest.mark.asyncio
async def test_reserve_uses_current_publication_policy_and_lossless_minor_units(installed):
    app, root = installed
    key, subject, _ = await register(app, 'field-reserve')
    amount = 9_007_199_254_740_993
    await apply_money(app, root, action='mint', operator='fixture', amount_minor=amount)
    await apply_money(
        app, root, action='transfer', operator='fixture', subject_id=subject, amount_minor=amount
    )
    assert (await star(app, subject))['balance'] == {'visibility': 'private'}
    result = await call(
        app, 'money.visibility_set', {'visibility': 'public'}, key=key, subject=subject
    )
    assert result.status == 'ok', wire(result)
    assert (await star(app, subject))['balance'] == {
        'visibility': 'public',
        'amount_minor': str(amount),
        'scale': 6,
        'code': app.settings.money.code,
    }
    result = await call(
        app, 'money.visibility_set', {'visibility': 'private'}, key=key, subject=subject
    )
    assert result.status == 'ok'
    assert (await star(app, subject))['balance'] == {'visibility': 'private'}


@pytest.mark.asyncio
async def test_root_anchor_refresh_boundaries_and_hidden_user_removal(installed):
    app, _ = installed
    _, subject, _ = await register(app, 'field-bounds')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        first = await http.get('/_universe?kind=users')
        assert first.status_code == 200, first.text
        assert first.json()['anchor']['id'] == ROOT_SUBJECT
        assert first.json()['anchor']['star']['role'] == 'root'
        refreshed = await http.get('/_universe', params={'ids': subject})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()['items'][0]['id'] == subject
        assert refreshed.json()['items'][0]['star']['role'] == 'user'
        assert 'anchor' not in refreshed.json()
        for query in (
            {'ids': ''},
            {'ids': subject + ',' + subject},
            {'ids': ','.join('u_' + str(i) for i in range(101))},
            {'ids': subject, 'cursor': 'invalid'},
            {'ids': subject, 'kind': 'posts'},
            {'ids': '/@field-bounds'},
            {'ids': subject, 'author': subject},
        ):
            assert (await http.get('/_universe', params=query)).status_code == 400
        assert (await http.head('/_universe', params={'ids': subject})).content == b''
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(subject)
            await tx.replace(
                replace(resource, mode=0o700, generation=resource.generation + 1),
                resource.generation,
            )
        hidden = await http.get('/_universe', params={'ids': subject})
        assert hidden.status_code == 200 and hidden.json()['items'] == []
        assert subject not in hidden.text


@pytest.mark.asyncio
async def test_owner_reserve_requires_current_browser_session_and_never_joins_public(oauth):
    app, _, subject, http = oauth
    assert (await http.get('/_universe/me')).json()['account'] is None
    await browser_login(oauth)
    private = await http.get('/_universe/me')
    assert private.status_code == 200, private.text
    account = private.json()['account']
    assert account['id'] == subject
    assert account['star']['balance'] == {
        'visibility': 'self',
        'amount_minor': '0',
        'scale': 6,
        'code': app.settings.money.code,
    }
    public = await http.get('/_universe', params={'ids': subject})
    assert public.json()['items'][0]['star']['balance'] == {'visibility': 'private'}
    assert 'self' not in public.json()['items'][0]['star']['balance']
    assert 'no-store' in private.headers['cache-control']
    http.cookies.clear()
    assert (await http.get('/_universe/me')).json()['account'] is None
