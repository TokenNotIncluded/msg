"""GET-only direct writes use the same request and authorization path as POST."""
import hashlib
import os
from datetime import timedelta
from urllib.parse import quote

import httpx
import pytest
from test_service import NOW

from msg.core.codec import b64, wire
from msg.core.models import Credential
from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app


def segment(value):
    return quote(str(value), safe='')


def direct_path(dictionary, operation, kind, *parts, arguments):
    code = dictionary.code_for('operation', operation + '@1')
    fields = []
    for name, value in arguments.items():
        fields.extend((dictionary.code_for('field', operation + '@1:' + name), value))
    return '/-/g/' + '/'.join(map(segment, (code, kind, *parts, 'args', *fields)))


@pytest.mark.asyncio
async def test_get_only_bootstrap_and_token_write_are_idempotent(installed):
    app, _ = installed
    dictionary = build_dictionary(app.registry)
    nonce = b64(os.urandom(32))
    expiry = wire(NOW + timedelta(seconds=120))
    bootstrap = direct_path(dictionary, 'identity.temporary', 'bootstrap',
                            'get-bootstrap', expiry, arguments={'nonce': nonce})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        assert (await http.head(bootstrap)).status_code == 200
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM identities WHERE kind='subject'")[0] == 2
        created = await http.get(bootstrap)
        assert created.status_code == 200, created.text
        assert created.json()['status'] == 'ok'
        assert created.json()['data']['token']
        query = await http.get(bootstrap + '?ignored=1')
        assert query.status_code == 400
        assert query.json()['error']['code'] == 'unknown_query_parameter'
        mixed = await http.get(bootstrap, headers={'X-Msg-Request': 'unused'})
        assert mixed.status_code == 400
        assert mixed.json()['error']['code'] == 'ambiguous_proof'
        replay = await http.get(bootstrap)
        assert replay.status_code == 200 and replay.json()['replayed'], replay.text
        assert replay.json()['data']['token'] == created.json()['data']['token']
        refreshed_bootstrap = direct_path(dictionary, 'identity.temporary', 'bootstrap',
                                          'get-bootstrap', wire(NOW + timedelta(seconds=180)),
                                          arguments={'nonce': nonce})
        assert (await http.get(refreshed_bootstrap)).json()['replayed'] is True

        uid = created.json()['data']['subject_id']
        credential = created.json()['data']['credential_id']
        token = created.json()['data']['token']
        post = direct_path(dictionary, 'content.post_create', 'token', credential, token,
                           uid, 'get-post', expiry,
                           arguments={'parent': '/tmp', 'body': 'GET only 写入 / unicode'})
        assert (await http.head(post)).status_code == 200
        first = await http.get(post)
        assert first.status_code == 200, first.text
        assert first.json()['status'] == 'ok'
        again = await http.get(post)
        assert again.status_code == 200 and again.json()['replayed'], again.text
        assert again.json()['resources'] == first.json()['resources']
        refreshed_post = direct_path(dictionary, 'content.post_create', 'token', credential,
                                     token, uid, 'get-post', wire(NOW + timedelta(seconds=180)),
                                     arguments={'parent': '/tmp', 'body': 'GET only 写入 / unicode'})
        assert (await http.get(refreshed_post)).json()['replayed'] is True
        changed = direct_path(dictionary, 'content.post_create', 'token', credential, token,
                              uid, 'get-post', expiry,
                              arguments={'parent': '/tmp', 'body': 'changed'})
        conflict = await http.get(changed)
        assert conflict.status_code == 409
        assert conflict.json()['error']['code'] == 'idempotency_conflict'
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 1


@pytest.mark.asyncio
async def test_get_only_write_rejects_expired_wrong_scope_and_root_token(installed):
    app, _ = installed
    dictionary = build_dictionary(app.registry)
    nonce = b64(os.urandom(32))
    expiry = wire(NOW + timedelta(seconds=120))
    bootstrap = direct_path(dictionary, 'identity.temporary', 'bootstrap',
                            'get-auth-bootstrap', expiry, arguments={'nonce': nonce})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        data = (await http.get(bootstrap)).json()['data']
        uid, credential, token = (data['subject_id'], data['credential_id'], data['token'])
        expired = direct_path(dictionary, 'content.post_create', 'token', credential, token,
                              uid, 'get-expired', wire(NOW - timedelta(seconds=1)),
                              arguments={'parent': '/tmp', 'body': 'expired'})
        response = await http.get(expired)
        assert response.status_code == 401 and response.json()['error']['code'] == 'request_expired'

        outside = direct_path(dictionary, 'content.post_create', 'token', credential, token,
                              uid, 'get-outside', expiry,
                              arguments={'parent': '/admins', 'body': 'outside'})
        denied = await http.get(outside)
        assert denied.status_code == 403
        assert denied.json()['error']['code'] in {'credential_ceiling', 'permission_denied'}

        wrong_token = direct_path(dictionary, 'content.post_create', 'token',
                                  credential, b64(os.urandom(32)), uid, 'get-wrong-token', expiry,
                                  arguments={'parent': '/tmp', 'body': 'wrong token'})
        invalid = await http.get(wrong_token)
        assert invalid.status_code == 401
        assert invalid.json()['error']['code'] == 'invalid_token'

        missing_id = direct_path(dictionary, 'content.post_create', 'token',
                                 credential, token, uid, '', expiry,
                                 arguments={'parent': '/tmp', 'body': 'missing ID'})
        bad_id = await http.get(missing_id)
        assert bad_id.status_code == 400
        assert bad_id.json()['error']['code'] == 'invalid_direct_write_metadata'

        wrong_subject = direct_path(dictionary, 'content.post_create', 'token',
                                    credential, token, 'u_root', 'get-wrong-subject', expiry,
                                    arguments={'parent': '/tmp', 'body': 'wrong subject'})
        subject_denied = await http.get(wrong_subject)
        assert subject_denied.status_code in {400, 403}
        assert subject_denied.json()['error']['code'] in {'local_only', 'delegation_required'}

        root_token = os.urandom(32)
        async with app.metadata.transaction(write=True) as tx:
            subject = await tx.subject('u_root')
            await tx.save_credential(Credential(
                id='t_test_root_get', subject_id='u_root', kind='token',
                verifier=hashlib.sha256(root_token).digest(),
                ceiling=app.temporary_ceiling(), not_before=NOW,
                expires_at=NOW + timedelta(hours=1), revoked_at=None),
                subject.auth_version)
        root_path = direct_path(dictionary, 'content.post_create', 'token',
                                't_test_root_get', b64(root_token), 'u_root',
                                'get-root', expiry,
                                arguments={'parent': '/tmp', 'body': 'root'})
        root_denied = await http.get(root_path)
        assert root_denied.status_code == 403
        assert root_denied.json()['error']['code'] == 'local_only'

        malformed = await http.get(outside.replace('/token/', '/bootstrap/', 1))
        assert malformed.status_code == 400
        assert malformed.json()['error']['code'] != 'internal_error'


@pytest.mark.asyncio
async def test_get_only_token_rotation_replay_and_revocation(installed):
    app, _ = installed
    dictionary = build_dictionary(app.registry)
    expiry = wire(NOW + timedelta(seconds=120))
    bootstrap = direct_path(dictionary, 'identity.temporary', 'bootstrap',
                            'get-rotate-bootstrap', expiry,
                            arguments={'nonce': b64(os.urandom(32))})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        data = (await http.get(bootstrap)).json()['data']
        uid, credential, token = data['subject_id'], data['credential_id'], data['token']
        rotate = direct_path(dictionary, 'identity.token_rotate', 'token',
                             credential, token, uid, 'get-rotate', expiry,
                             arguments={'nonce': b64(os.urandom(32))})
        first = await http.get(rotate)
        assert first.status_code == 200, first.text
        replay = await http.get(rotate)
        assert replay.status_code == 200 and replay.json()['replayed'], replay.text
        assert replay.json()['data']['token'] == first.json()['data']['token']
        old = direct_path(dictionary, 'content.post_create', 'token',
                          credential, token, uid, 'old-token-post', expiry,
                          arguments={'parent': '/tmp', 'body': 'old token'})
        revoked = await http.get(old)
        assert revoked.status_code == 401
        assert revoked.json()['error']['code'] == 'credential_revoked'
        fresh = direct_path(dictionary, 'content.post_create', 'token',
                            first.json()['data']['credential_id'],
                            first.json()['data']['token'], uid, 'fresh-token-post', expiry,
                            arguments={'parent': '/tmp', 'body': 'new token'})
        accepted = await http.get(fresh)
        assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
async def test_get_only_expected_generation_uses_executor_conflict_check(installed):
    app, _ = installed
    dictionary = build_dictionary(app.registry)
    expiry = wire(NOW + timedelta(seconds=120))
    bootstrap = direct_path(dictionary, 'identity.temporary', 'bootstrap',
                            'get-expected-bootstrap', expiry,
                            arguments={'nonce': b64(os.urandom(32))})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        data = (await http.get(bootstrap)).json()['data']
        prefix = (data['credential_id'], data['token'], data['subject_id'])
        stale = direct_path(dictionary, 'content.post_create', 'token', *prefix,
                            'expected-stale', expiry, 'expected', 't_tmp', '999',
                            arguments={'parent': '/tmp', 'body': 'stale'})
        conflict = await http.get(stale)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()['error']['code'] == 'generation_conflict'
        async with app.metadata.transaction(write=False) as tx:
            generation = (await tx.resource('t_tmp')).generation
        current = direct_path(dictionary, 'content.post_create', 'token', *prefix,
                              'expected-current', expiry, 'expected', 't_tmp', generation,
                              arguments={'parent': '/tmp', 'body': 'current'})
        accepted = await http.get(current)
        assert accepted.status_code == 200, accepted.text
