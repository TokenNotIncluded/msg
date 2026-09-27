"""Short and long subject paths share live read authorization and content."""
from datetime import timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app
from msg.security.crypto import Ed25519Signer
from msg.security.capabilities import grant_for
from msg.core.codec import wire
from test_service import NOW, call, register


@pytest.mark.asyncio
async def test_resource_backed_subject_aliases_share_content_cache_and_permissions(installed):
    app, _ = installed
    key, user, certificate_id = await register(app, 'alias-owner')
    ssh=Ed25519Signer.generate()
    public=Ed25519PrivateKey.from_private_bytes(ssh.private_bytes()).public_key().public_bytes(
        serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    proof=ssh.sign(canonical({'subject_id':user,'public_key':public}),purpose='ssh-key-add')
    added=await call(app,'identity.ssh_key_add',{'public_key':public,'proof':wire(proof),
        'ceiling':wire((grant_for(app.registry.capability('git.basic')),))},key=key,subject=user)
    assert added.status=='ok',wire(added)
    stored=await call(app,'keystore.put',{'name':'sealed','format':'age',
        'ciphertext':b64(b'age-encryption.org/v1\npayload')},key=key,subject=user)
    assert stored.status=='ok',wire(stored)
    before=None
    async with app.metadata.transaction(write=False) as tx:
        before=(tx.one('SELECT COUNT(*) FROM events')[0],
                tx.one('SELECT SUM(generation) FROM resources')[0])
        keystore=await tx.resolve('/@alias-owner/keystore')
    signed=request_for('discovery.get',{'id':keystore},app.settings.service_url,
                       signer=key,subject=user,expires_at=NOW+timedelta(seconds=120))
    headers={'X-Msg-Request':b64(canonical(signed))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for short,long in (('cert','certificates'),('ssh','ssh-keys')):
            left=await http.get('/@alias-owner/'+short+'/json')
            right=await http.get('/@alias-owner/'+long+'/json')
            assert left.status_code==right.status_code==200
            assert left.content==right.content
            assert left.headers['etag']==right.headers['etag']
            assert not left.is_redirect and not right.is_redirect
            cached_short=await http.get('/@alias-owner/'+short+'/json',
                                        headers={'If-None-Match':left.headers['etag']})
            cached_long=await http.get('/@alias-owner/'+long+'/json',
                                       headers={'If-None-Match':right.headers['etag']})
            assert cached_short.status_code==cached_long.status_code==304
            if short=='cert':
                assert left.json()['path']=='/@alias-owner/'+short
                detail=await http.get('/@alias-owner/cert/'+certificate_id)
                legacy=await http.get('/@alias-owner/certificates/'+certificate_id)
                assert detail.status_code==legacy.status_code==200
                assert detail.content==legacy.content
            else:
                assert [item['key_id'] for item in left.json()['keys']]==[added.data['key_id']]
                detail=await http.get('/@alias-owner/ssh/'+added.data['key_id'])
                legacy=await http.get('/@alias-owner/ssh-keys/'+added.data['key_id'])
                assert detail.status_code==legacy.status_code==200
                assert detail.content==legacy.content
        for short,long in (('ks','keystore'),):
            denied_short=await http.get('/@alias-owner/'+short+'/json')
            denied_long=await http.get('/@alias-owner/'+long+'/json')
            assert denied_short.status_code==denied_long.status_code==403
            assert denied_short.json()['error']['code']==denied_long.json()['error']['code']
            left=await http.get('/@alias-owner/'+short+'/json',headers=headers)
            right=await http.get('/@alias-owner/'+long+'/json',headers=headers)
            assert left.status_code==right.status_code==200
            assert left.content==right.content
            assert left.headers['etag']==right.headers['etag']
            assert left.json()['path']=='/@alias-owner/ks'
            cached=await http.get('/@alias-owner/keystore/json',
                                  headers={**headers,'If-None-Match':left.headers['etag']})
            assert cached.status_code==304
            head=await http.head('/@alias-owner/ks/json',headers=headers)
            assert head.status_code==200 and head.headers['etag']==left.headers['etag']
            entry_packet=request_for('discovery.get',{'id':stored.resources[0].id},
                app.settings.service_url,signer=key,subject=user,
                expires_at=NOW+timedelta(seconds=120))
            entry_headers={'X-Msg-Request':b64(canonical(entry_packet))}
            entry_short=await http.get('/@alias-owner/ks/sealed/json',headers=entry_headers)
            entry_long=await http.get('/@alias-owner/keystore/sealed/json',headers=entry_headers)
            assert entry_short.status_code==entry_long.status_code==200
            assert entry_short.content==entry_long.content
            assert entry_short.json()['path']=='/@alias-owner/ks/sealed'
            anonymous=await http.get('/@alias-owner/ks/sealed/meta')
            assert anonymous.status_code==403
    async with app.metadata.transaction(write=False) as tx:
        assert before==(tx.one('SELECT COUNT(*) FROM events')[0],
                        tx.one('SELECT SUM(generation) FROM resources')[0])


@pytest.mark.asyncio
async def test_operation_backed_subject_aliases_use_same_live_handler(installed):
    app, _ = installed
    key, user, _ = await register(app, 'mailbox-alias')
    other_key, other, _ = await register(app, 'mailbox-other')
    for index in range(2):
        post=await call(app,'content.post_create',{'parent':'/main','body':f'notice {index}'},
                        key=other_key,subject=other)
        sent=await call(app,'communication.send',
                        {'recipient':user,'resource':wire(post.resources[0])},
                        key=other_key,subject=other)
        assert sent.status=='ok',wire(sent)
    async with app.metadata.transaction(write=False) as tx:
        before=(tx.one('SELECT COUNT(*) FROM events')[0],
                tx.one('SELECT COUNT(*) FROM jobs')[0],
                tx.one('SELECT COUNT(*) FROM revisions')[0],
                tx.one('SELECT SUM(generation) FROM resources')[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for short,long,operation in (('in','inbox','communication.inbox'),
                                     ('out','outbox','communication.outbox'),
                                     ('dm','dm','communication.dm_list')):
            packet=request_for(operation,{},app.settings.service_url,
                               signer=key,subject=user,expires_at=NOW+timedelta(seconds=120))
            headers={'X-Msg-Request':b64(canonical(packet))}
            left=await http.get('/@mailbox-alias/'+short,headers=headers)
            right=await http.get('/@mailbox-alias/'+long,headers=headers)
            assert left.status_code==right.status_code==200,(left.text,right.text)
            assert left.content==right.content
            assert left.headers['etag']==right.headers['etag']
            assert not left.is_redirect and not right.is_redirect
            cached=await http.get('/@mailbox-alias/'+long,
                                  headers={**headers,'If-None-Match':left.headers['etag']})
            assert cached.status_code==304
            denied=await http.get('/@mailbox-other/'+short,headers=headers)
            assert denied.status_code==403
        inbox=request_for('communication.inbox',{'limit':1},app.settings.service_url,
                          signer=key,subject=user,expires_at=NOW+timedelta(seconds=120))
        paged_headers={'X-Msg-Request':b64(canonical(inbox))}
        first=await http.get('/@mailbox-alias/in?limit=1',headers=paged_headers)
        old=await http.get('/@mailbox-alias/inbox?limit=1',headers=paged_headers)
        assert first.status_code==old.status_code==200
        assert first.content==old.content and first.json().get('cursor')
        for short,long in (('pk','pubkey'),('k','keys'),('ek','encryption-key'),
                           ('e','encryption-keys'),('ach','achievements')):
            public_short=await http.get('/@mailbox-alias/'+short)
            public_long=await http.get('/@mailbox-alias/'+long)
            assert public_short.status_code==public_long.status_code==200
            assert public_short.content==public_long.content
            assert public_short.headers['etag']==public_long.headers['etag']
            assert not public_short.is_redirect and not public_long.is_redirect
            assert public_short.json()['path']=='/@mailbox-alias/'+short
            cached=await http.get('/@mailbox-alias/'+long,
                                  headers={'If-None-Match':public_short.headers['etag']})
            assert cached.status_code==304
            if short in {'pk','ek','k','e'}:
                key_id=(public_short.json()['key_id'] if short in {'pk','ek'}
                        else public_short.json()['keys'][0]['key_id'])
                by_id=await http.get('/@mailbox-alias/'+short+'/'+key_id)
                old_by_id=await http.get('/@mailbox-alias/'+long+'/'+key_id)
                assert by_id.status_code==old_by_id.status_code==200
                assert by_id.content==old_by_id.content
    async with app.metadata.transaction(write=False) as tx:
        assert before==(tx.one('SELECT COUNT(*) FROM events')[0],
                        tx.one('SELECT COUNT(*) FROM jobs')[0],
                        tx.one('SELECT COUNT(*) FROM revisions')[0],
                        tx.one('SELECT SUM(generation) FROM resources')[0])


@pytest.mark.asyncio
async def test_key_aliases_fail_closed_for_missing_key_id(installed):
    app, _ = installed
    await register(app, 'missing-key-view')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for short,long in (('pk','pubkey'),('ek','encryption-key')):
            left=await http.get('/@missing-key-view/'+short+'/k_missing')
            right=await http.get('/@missing-key-view/'+long+'/k_missing')
            assert left.status_code==right.status_code
            assert left.json()['error']['code']==right.json()['error']['code']
