"""LFS objects never authorize themselves and never publish partial bytes."""
import base64
import hashlib
import os
from datetime import timedelta

import httpx
import pytest

from msg.core.codec import b64,canonical,wire
from msg.core.requests import request_for
from msg.security.capabilities import grant_for
from msg.transports.http import create_app
from test_service import NOW,call,register


@pytest.mark.asyncio
async def test_lfs_upload_only_under_execution_boundary_and_current_repo_acl(installed):
    app,_=installed
    key,user,_=await register(app,'lfs-owner')
    created=await call(app,'git.create',{'parent':'/@lfs-owner','name':'code.git'},key=key,subject=user)
    assert created.status=='ok',wire(created)
    rid=created.resources[0].id
    issued=await call(app,'identity.token_create',{'nonce':b64(os.urandom(32)),
        'ceiling':wire((grant_for(app.registry.capability('git.basic')),)),'ttl':3600},
        key=key,subject=user)
    basic='Basic '+base64.b64encode((issued.data['credential_id']+':'+issued.data['token']).encode()).decode()
    data=b'large-file-content\n'*100
    oid=hashlib.sha256(data).hexdigest()
    batch={'operation':'upload','objects':[{'oid':oid,'size':len(data)}]}
    read=created.data['read_url'].removeprefix(app.settings.service_url.rstrip('/'))
    bypass=await call(app,'git.lfs_publish',{'id':rid,'oid':oid,'size':len(data)},
                      key=key,subject=user)
    assert bypass.error.code=='lfs_transport_required'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        ordinary=await http.post(read+'/info/lfs/objects/batch',json=batch,
                                 headers={'Content-Type':'application/vnd.git-lfs+json',
                                          'Authorization':basic})
        assert ordinary.status_code==405
        planned=await http.post(f'/-/git/{rid}/info/lfs/objects/batch',json=batch,
                                headers={'Content-Type':'application/vnd.git-lfs+json',
                                         'Authorization':basic})
        assert planned.status_code==200,planned.text
        upload=planned.json()['objects'][0]['actions']['upload']['href']
        assert upload.startswith(app.settings.service_url.rstrip('/')+'/-/git/')
        path=upload.removeprefix(app.settings.service_url.rstrip('/'))
        absent=await http.get(read+'/info/lfs/objects/'+oid)
        assert absent.status_code==404
        wrong=await http.put(path,content=data[:-1]+b'x',headers={'Authorization':basic})
        assert wrong.status_code==400
        assert not list(app.settings.server.staging_dir.glob('msg-lfs-*'))
        short=await http.put(path,content=data[:-1],headers={'Authorization':basic})
        assert short.status_code==400
        assert not list(app.settings.server.staging_dir.glob('msg-lfs-*'))
        still_absent=await http.get(read+'/info/lfs/objects/'+oid)
        assert still_absent.status_code==404
        done=await http.put(path,content=data,headers={'Authorization':basic})
        assert done.status_code==200,done.text
        downloaded=await http.get(read+'/info/lfs/objects/'+oid)
        assert downloaded.status_code==200 and downloaded.content==data
        missing_auth=await http.put(path,content=data)
        assert missing_auth.status_code==401


@pytest.mark.asyncio
async def test_lfs_ordinary_download_batch_is_read_only(installed):
    app,_=installed
    key,user,_=await register(app,'lfs-reader')
    created=await call(app,'git.create',{'parent':'/@lfs-reader','name':'code.git'},key=key,subject=user)
    rid=created.resources[0].id
    oid='0'*64
    path=created.data['read_url'].removeprefix(app.settings.service_url.rstrip('/'))
    async with app.metadata.transaction(write=False) as tx:
        before=tx.one('SELECT COUNT(*) FROM events')[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        result=await http.post(path+'/info/lfs/objects/batch',
            json={'operation':'download','objects':[{'oid':oid,'size':4}]},
            headers={'Content-Type':'application/vnd.git-lfs+json'})
        assert result.status_code==200,result.text
        assert result.json()['objects'][0]['error']['code']==404
        assert (await http.get(path+'/info/lfs/objects/'+oid)).status_code==404
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0]==before
    assert not app.settings.server.repositories_dir.joinpath(rid+'.git','lfs').exists()


@pytest.mark.asyncio
async def test_lfs_signed_put_uses_one_publish_proof(installed):
    app,_=installed
    key,user,_=await register(app,'lfs-signed')
    created=await call(app,'git.create',{'parent':'/@lfs-signed','name':'code.git'},
                       key=key,subject=user)
    rid=created.resources[0].id
    body=b'signed-lfs-body\n'
    oid=hashlib.sha256(body).hexdigest()
    path=f'/-/git/{rid}/info/lfs/objects/{oid}/{len(body)}'
    packet=request_for('git.lfs_publish',{'id':rid,'oid':oid,'size':len(body)},
                       app.settings.service_url,signer=key,subject=user,
                       expires_at=NOW+timedelta(seconds=120))
    headers={'X-Msg-Request':b64(canonical(packet))}
    wrong=request_for('git.lfs_write_authorize',{'id':rid},app.settings.service_url,
                      signer=key,subject=user,expires_at=NOW+timedelta(seconds=120))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        rejected=await http.put(path,content=body,
                                headers={'X-Msg-Request':b64(canonical(wrong))})
        assert rejected.status_code==400
        assert not list(app.settings.server.staging_dir.glob('msg-lfs-*'))
        accepted=await http.put(path,content=body,headers=headers)
        assert accepted.status_code==200,accepted.text
        fetched=await http.get(created.data['read_url'].removeprefix(app.settings.service_url.rstrip('/'))+
                               '/info/lfs/objects/'+oid)
        assert fetched.status_code==200 and fetched.content==body
