"""Resource-bound MCP OAuth, with real PostgreSQL and the existing login fixture."""

import gzip
import hashlib
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import test_oauth
from test_oauth import browser_login, device_tokens
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, loads, unb64, wire
from msg.core.requests import request_for
from msg.oauth_config import CHATGPT_CLIENT, LEGACY_SCOPES, MCP_SCOPE_OPERATIONS, load_oauth
from msg.security.oauth import get, resource_execution, save
from msg.transports.http_common import body_bytes
from msg.transports.mcp_auth import (
    MCPIdentity,
    authenticate_mcp,
    challenge,
    protected_resource_metadata,
)
from msg.transports.oauth_http import OAuthBoundary, csrf

oauth = test_oauth.oauth


@pytest.fixture
async def mcp_oauth(oauth):
    app, key, subject, http = oauth
    app.settings = replace(
        app.settings,
        oauth=replace(app.settings.oauth, clients=(*app.settings.oauth.clients, CHATGPT_CLIENT)),
    )
    app.authenticator.oauth_config = app.settings.oauth
    return app, key, subject, http


async def mcp_code(context, scope='msg.mcp.read offline_access', *, decision='approve'):
    app, _, _, http = context
    cookie = await browser_login(context)
    verifier = 'm' * 64
    args = {
        'client_id': 'msg-chatgpt',
        'response_type': 'code',
        'redirect_uri': CHATGPT_CLIENT.redirect_uris[0],
        'scope': scope,
        'resource': app.settings.service_url + '/-/mcp',
        'state': 'state-with-enough-entropy',
        'code_challenge_method': 'S256',
        'code_challenge': b64(hashlib.sha256(verifier.encode()).digest()),
    }
    response = await http.post(
        '/oauth/authorize',
        data=dict(args, csrf=csrf(cookie), decision=decision),
        headers={'Origin': app.settings.service_url},
    )
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['iss'] == [app.settings.service_url]
    assert query['state'] == [args['state']]
    return args, verifier, query


async def mcp_tokens(context, scope='msg.mcp.read offline_access'):
    app, _, _, http = context
    args, verifier, query = await mcp_code(context, scope)
    data = {
        'client_id': args['client_id'],
        'grant_type': 'authorization_code',
        'code': query['code'][0],
        'redirect_uri': args['redirect_uri'],
        'code_verifier': verifier,
        'resource': args['resource'],
    }
    response = await http.post('/oauth/token', data=data)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['resource'] == args['resource']
    return result


async def identity_response(
    context, token=None, *, path='/-/mcp', body=None, method='POST', headers=None, compressed=False
):
    app, _, _, _ = context

    async def downstream(scope, receive, send):
        from starlette.requests import Request

        message = loads(
            await body_bytes(Request(scope, receive), app.settings.server.limits.max_request_bytes)
        )
        assert isinstance(message, dict)
        identity = scope['state']['msg_mcp_identity']
        # Only explicitly selected public facts ever leave the request context.
        payload = {'authenticated': identity is not None}
        if identity is not None:
            payload.update(subject=identity.subject, operations=sorted(identity.operations))
        from starlette.responses import JSONResponse

        await JSONResponse(payload)(scope, receive, send)

    boundary = OAuthBoundary(downstream, service=app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary), base_url=app.settings.service_url
    ) as http:
        http.cookies.set('msg_session', 'this-cookie-is-not-an-mcp-credential')
        values = {'Authorization': 'Bearer ' + token} if token is not None else {}
        values.update(headers or {})
        message = body or {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        payload = {'json': message}
        if compressed:
            values['Content-Encoding'] = 'gzip'
            payload = {'content': gzip.compress(canonical(message))}
        return await http.request(method, path, headers=values, **payload)


def test_mcp_client_defaults_and_exact_redirects():
    config = load_oauth({'enabled': True}, 'https://msg.example')
    cli = next(c for c in config.clients if c.client_id == 'msg-cli')
    assert cli.scopes == LEGACY_SCOPES
    chatgpt = next(c for c in config.clients if c.client_id == 'msg-chatgpt')
    assert chatgpt == CHATGPT_CLIENT
    assert chatgpt.redirect_uris == ('https://chatgpt.com/connector_platform_oauth_redirect',)
    assert not chatgpt.scopes & {'msg.read', 'msg.write'}
    assert all(
        not operation.startswith(('identity.', 'root.', 'system.', 'money.', 'cert.'))
        for operations in MCP_SCOPE_OPERATIONS.values()
        for operation in operations
    )


@pytest.mark.asyncio
async def test_mcp_discovery_issuer_pkce_and_deny(mcp_oauth):
    app, _, _, http = mcp_oauth
    for path in (
        '/.well-known/oauth-protected-resource',
        '/.well-known/oauth-protected-resource/-/mcp',
        '/.well-known/oauth-protected-resource/-/mcp/raw',
    ):
        response = await http.get(path)
        assert response.status_code == 200, response.text
        assert response.json() == protected_resource_metadata(app)
        assert (await http.post(path)).status_code == 405
    metadata = (await http.get('/.well-known/oauth-authorization-server')).json()
    assert metadata['authorization_response_iss_parameter_supported'] is True
    assert metadata['code_challenge_methods_supported'] == ['S256']
    assert metadata['token_endpoint_auth_methods_supported'] == ['none']
    assert 'registration_endpoint' not in metadata
    assert not metadata.get('client_id_metadata_document_supported', False)
    _, _, query = await mcp_code(mcp_oauth, decision='deny')
    assert query['error'] == ['access_denied'] and 'code' not in query
    args, _, _ = await mcp_code(mcp_oauth)
    for changed, code in [
        ({'resource': 'https://other.example/-/mcp'}, 'invalid_target'),
        ({'resource': app.settings.service_url + '/-/mcp/raw'}, 'invalid_target'),
        ({'resource': ''}, 'invalid_target'),
        ({'redirect_uri': CHATGPT_CLIENT.redirect_uris[0] + '/other'}, 'invalid_redirect_uri'),
        ({'code_challenge_method': 'plain'}, 'invalid_request'),
    ]:
        response = await http.get('/oauth/authorize?' + urlencode(dict(args, **changed)))
        assert response.status_code == 400 and response.json()['error'] == code
    args.pop('resource')
    response = await http.get('/oauth/authorize?' + urlencode(args))
    assert response.status_code == 400 and response.json()['error'] == 'invalid_target'


@pytest.mark.asyncio
async def test_mcp_identity_frozen_scope_and_secret_projection(mcp_oauth):
    app, _, subject, _ = mcp_oauth
    tokens = await mcp_tokens(mcp_oauth)
    identity = await authenticate_mcp(app, 'Bearer ' + tokens['access_token'])
    assert isinstance(identity, MCPIdentity) and identity.subject == subject
    assert identity.credential_type == 'oauth' and identity.session_binding.startswith('family:')
    assert identity.operations <= MCP_SCOPE_OPERATIONS['msg.mcp.read']
    assert 'discovery.get@1' in identity.operations
    assert not identity.operations & {'content.post_create@2', 'discussion.ack@1'}
    with pytest.raises(FrozenInstanceError):
        identity.subject = 'other'
    encoded = tokens['access_token'].partition('.')[2]
    assert encoded not in repr(identity) and str(identity.token) not in repr(identity)
    for path in ('/-/mcp', '/-/mcp/raw'):
        response = await identity_response(mcp_oauth, tokens['access_token'], path=path)
        assert response.status_code == 200 and response.json()['subject'] == subject
        assert encoded not in response.text and 'credential_id' not in response.text
        assert (await identity_response(mcp_oauth, path=path)).json() == {'authenticated': False}


@pytest.mark.asyncio
async def test_mcp_wrong_audience_legacy_cookie_and_mixed_proof(mcp_oauth):
    app, _, subject, http = mcp_oauth
    legacy = await device_tokens(mcp_oauth)
    assert (await identity_response(mcp_oauth, legacy['access_token'])).status_code == 401
    assert (await identity_response(mcp_oauth)).json() == {'authenticated': False}
    tokens = await mcp_tokens(mcp_oauth)
    assert (
        await identity_response(mcp_oauth, tokens['access_token'], compressed=True)
    ).status_code == 200
    mixed = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {
            'name': 'discovery.get',
            'arguments': {'packet': {'proof': {'token': 'other-secret'}}},
        },
    }
    response = await identity_response(mcp_oauth, tokens['access_token'], body=mixed)
    assert response.status_code == 400 and response.json()['error'] == 'ambiguous_credentials'
    assert 'other-secret' not in response.text
    mixed['params']['arguments']['packet'] = canonical(
        mixed['params']['arguments']['packet']
    ).decode()
    response = await identity_response(mcp_oauth, tokens['access_token'], body=mixed)
    assert response.status_code == 400 and response.json()['error'] == 'ambiguous_credentials'
    assert (
        await identity_response(mcp_oauth, tokens['access_token'], method='GET')
    ).status_code == 405
    assert (
        await identity_response(
            mcp_oauth, tokens['access_token'], headers={'x-msg-request': 'proof'}
        )
    ).status_code == 400
    # A token for the MCP audience is not accepted by the generic bearer adapter.
    packet = request_for(
        'discovery.get', {'id': '/main'}, app.settings.service_url, subject=subject
    )
    response = await http.post(
        '/-/p/discovery.get',
        content=canonical(packet),
        headers={'Authorization': 'Bearer ' + tokens['access_token']},
    )
    assert response.status_code == 401
    bound_packet = request_for(
        'discovery.get',
        {'id': '/main'},
        app.settings.service_url,
        subject=subject,
        token=(
            tokens['access_token'].partition('.')[0],
            unb64(tokens['access_token'].partition('.')[2]),
        ),
        expires_at=app.clock() + timedelta(minutes=3),
    )
    response = await http.post('/-/p/discovery.get', content=canonical(bound_packet))
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'invalid_grant'
    for token in ('malformed', 't_oauth_missing.bad', tokens['access_token'] + '!', ' '):
        response = await identity_response(mcp_oauth, token)
        assert response.status_code == 401, response.text
        assert response.headers['www-authenticate'] == challenge(
            app, 'invalid_token', ('msg.mcp.read',)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mutation', ['expiry', 'token', 'parent', 'family', 'authority', 'resource']
)
async def test_mcp_live_revocation_and_resource_fence(mcp_oauth, mutation):
    app, key, subject, _ = mcp_oauth
    tokens = await mcp_tokens(mcp_oauth)
    identity = await authenticate_mcp(app, 'Bearer ' + tokens['access_token'])
    if mutation == 'expiry':
        app._oauth_clock[0] = NOW + timedelta(seconds=901)
    else:
        async with app.metadata.transaction(write=True) as tx:
            if mutation in {'token', 'parent'}:
                credential = await tx.credential(
                    identity.credential_id if mutation == 'token' else key.key_id
                )
                user = await tx.subject(subject)
                await tx.save_credential(replace(credential, revoked_at=NOW), user.auth_version)
            elif mutation in {'family', 'resource'}:
                _, family = get(tx, identity.session_binding, app.clock())
                if mutation == 'family':
                    family['revoked'] = True
                else:
                    family['resource'] = 'https://other.example/-/mcp'
                save(tx, identity.session_binding, family)
            else:
                user = await tx.subject(subject)
                await tx.update_identity(
                    replace(user, auth_version=user.auth_version + 1), user.auth_version
                )
    response = await identity_response(mcp_oauth, tokens['access_token'])
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_mcp_refresh_binding_no_expansion_and_scope_narrowing(mcp_oauth):
    app, _, _, http = mcp_oauth
    tokens = await mcp_tokens(mcp_oauth, 'msg.mcp.read msg.mcp.post offline_access')
    args = {
        'client_id': 'msg-chatgpt',
        'grant_type': 'refresh_token',
        'refresh_token': tokens['refresh_token'],
        'resource': app.settings.service_url + '/-/mcp',
    }
    for patch, code in [
        ({'resource': 'https://other.example'}, 'invalid_target'),
        ({'scope': 'msg.mcp.read msg.mcp.message msg.mcp.post offline_access'}, 'invalid_scope'),
    ]:
        response = await http.post('/oauth/token', json=dict(args, **patch))
        assert response.status_code == 400 and response.json()['error'] == code
    missing = dict(args)
    missing.pop('resource')
    assert (await http.post('/oauth/token', json=missing)).json()['error'] == 'invalid_target'
    response = await http.post('/oauth/token', json=dict(args, scope='msg.mcp.read offline_access'))
    assert response.status_code == 200, response.text
    new = response.json()
    identity = await authenticate_mcp(app, 'Bearer ' + new['access_token'])
    assert identity.operations <= MCP_SCOPE_OPERATIONS['msg.mcp.read']
    assert (await identity_response(mcp_oauth, tokens['access_token'])).status_code == 401
    replay = await http.post('/oauth/token', json=args)
    assert replay.json()['error'] == 'invalid_grant'
    assert (await identity_response(mcp_oauth, new['access_token'])).status_code == 401


@pytest.mark.asyncio
async def test_mcp_readonly_cannot_write_and_cross_account_acl(mcp_oauth):
    app, _, subject, _ = mcp_oauth
    tokens = await mcp_tokens(mcp_oauth)
    credential, _, value = tokens['access_token'].partition('.')
    proof = (credential, unb64(value))
    other_key, other, _ = await register(app, 'mcp-other')
    with resource_execution(app.settings.service_url + '/-/mcp'):
        result = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': 'not allowed'},
            token=proof,
            subject=subject,
            contract_version=2,
        )
        assert result.status == 'error' and result.error.code == 'credential_ceiling'
        result = await call(
            app, 'discovery.get', {'id': other, 'fields': ['id']}, token=proof, subject=other
        )
        assert result.status == 'error' and result.error.code in {
            'delegation_required',
            'subject_mismatch',
        }
        own = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': 'private'},
            key=other_key,
            subject=other,
        )
        assert own.status == 'ok', wire(own)
        private = await call(
            app,
            'content.chmod',
            {'id': own.resources[0].id, 'mode': '0600'},
            key=other_key,
            subject=other,
            expected=((own.resources[0].id, own.data['generation']),),
        )
        assert private.status == 'ok', wire(private)
        result = await call(
            app,
            'discovery.get',
            {'id': own.resources[0].id, 'fields': ['content']},
            token=proof,
            subject=subject,
        )
        assert result.status == 'error' and result.error.code in {
            'permission_denied',
            'access_denied',
        }
