"""End-to-end requests go through real signature verification and SQLite commits."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os

import pytest

from msg.application import Application
from msg.admin.root import _provision, _approve_csr
from msg.config import write_example
from msg.constants import ROOT_SUBJECT
from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for, receipt_bytes
from msg.security.crypto import Ed25519Signer, subject_id, verify

NOW = datetime(2026, 9, 27, tzinfo=UTC)


from conftest import installed


async def call(app, op, args, *, key=None, subject=None, certs=(), expected=(), rid=None, token=None):
    packet = request_for(op, args, app.settings.service_url,
        signer=key, subject=subject, certificates=certs, expected=expected,
        request_id=rid, token=token, expires_at=NOW+timedelta(seconds=120))
    return await app.executor.execute(packet)


async def register(app, handle):
    key = Ed25519Signer.generate()
    uid = subject_id(key.public_key)
    result = await call(app, 'identity.register', {'handle':handle,'public_key':b64(key.public_key)}, key=key, subject=uid)
    assert result.status == 'ok', wire(result)
    return key, uid, result.data['certificate_id']


@pytest.mark.asyncio
async def test_service_register_post_read_reply_receipt(installed):
    app, root = installed
    key, uid, cert = await register(app, 'alice')
    async def invoke(op, args, **kw):
        return await call(app, op, args, key=key, subject=uid, certs=(cert,), **kw)
    result = await invoke('content.post_create', {'parent':'/main','body':'A short message.'}, rid='post-once')
    assert result.status == 'ok', wire(result)
    verify(app.receipt_signer.public_key, receipt_bytes(result), result.receipt, purpose='receipt')
    repeated = await invoke('content.post_create', {'parent':'/main','body':'A short message.'}, rid='post-once')
    assert repeated.status == 'ok' and repeated.replayed
    conflict = await invoke('content.post_create', {'parent':'/main','body':'Different.'}, rid='post-once')
    assert conflict.error.code == 'idempotency_conflict'
    rid = result.resources[0].id
    read = await call(app, 'discovery.get', {'id':rid})
    assert read.status == 'ok', wire(read)
    assert 'A short message.' in str(read.data)
    reply = await invoke('discussion.reply', {'target':{'id':rid},'body':'Received.'})
    assert reply.status == 'ok', wire(reply)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(reply.resources[0].id)).type == 'post'
        before = tx.one('SELECT COUNT(*) FROM events')[0]
    await call(app, 'discovery.get', {'id':rid})
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0] == before


@pytest.mark.asyncio
async def test_root_network_forbidden_even_cli_source(installed):
    app, root = installed
    result = await call(app, 'discovery.get', {'id':'/private'}, key=root, subject=ROOT_SUBJECT)
    assert result.status == 'error' and result.error.code == 'local_only', wire(result)
    assert not hasattr(app, 'root_signer')


@pytest.mark.asyncio
async def test_default_deny_and_certgate(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'bob')
    for path in ('/private','/admins','/_tools/curl'):
        result = await call(app,'discovery.get',{'id':path},key=key,subject=uid,certs=(cert,))
        assert result.status == 'error', (path,wire(result))
    result = await call(app,'content.post_create',{'parent':'/certified','body':'gate'},key=key,subject=uid,certs=(cert,))
    assert result.error.code == 'certificate_gate', wire(result)


@pytest.mark.asyncio
async def test_temporary_rotation_and_upgrade_preserve_subject(installed):
    app, _ = installed
    result = await call(app,'identity.temporary',{'nonce':b64(os.urandom(32))})
    assert result.status == 'ok', wire(result)
    uid = result.data['subject_id']
    from msg.core.codec import unb64
    token = (result.data['credential_id'], unb64(result.data['token']))
    post = await call(app,'content.post_create',{'parent':'/tmp','body':'temporary'},subject=uid,token=token)
    assert post.status == 'ok', wire(post)
    forbidden = await call(app,'content.chmod',{'id':post.resources[0].id,'mode':'0777'},subject=uid,token=token,
                           expected=((post.resources[0].id,post.data['generation']),))
    assert forbidden.error.code == 'credential_ceiling'
    new_key = Ed25519Signer.generate()
    args = {'subject_id':uid,'public_key':b64(new_key.public_key),'handle':'promoted'}
    upgrade = await call(app,'identity.upgrade',{'handle':'promoted','public_key':args['public_key'],
        'possession_proof':wire(new_key.sign(canonical(args),purpose='upgrade'))},subject=uid,token=token)
    assert upgrade.status == 'ok' and upgrade.data['subject_id'] == uid, wire(upgrade)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(post.resources[0].id)).owner == uid

@pytest.mark.asyncio
async def test_requested_write_projection_is_returned_without_readback(installed):
    app,_=installed
    key,uid,_=await register(app,'projection-author')
    from msg.core.requests import request_for
    packet=request_for('content.post_create',{'parent':'/main','body':'project me'},app.settings.service_url,
        subject=uid,signer=key,expires_at=NOW+timedelta(seconds=120),return_fields=('id','revision','size'))
    result=await app.executor.execute(packet)
    assert result.status=='ok',result
    assert result.data['projection'][0]['size']==10
