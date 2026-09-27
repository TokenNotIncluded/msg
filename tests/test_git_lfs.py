"""LFS objects never authorize themselves and never publish partial bytes."""
import base64
import hashlib
import os
import asyncio
import subprocess
import shutil
from datetime import timedelta

import httpx
import pytest
import uvicorn

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
        'recovery_secret':b64(os.urandom(32)),
        'ceiling':wire((grant_for(app.registry.capability('git.basic')),)),'ttl':3600},
        key=key,subject=user,contract_version=2)
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
        assert downloaded.headers['accept-ranges']=='bytes'
        ranged=await http.get(read+'/info/lfs/objects/'+oid,headers={'Range':'bytes=1-5'})
        assert ranged.status_code==206 and ranged.content==data[1:6]
        assert ranged.headers['content-range']==f'bytes 1-5/{len(data)}'
        suffix=await http.get(read+'/info/lfs/objects/'+oid,headers={'Range':'bytes=-4'})
        assert suffix.status_code==206 and suffix.content==data[-4:]
        head=await http.head(read+'/info/lfs/objects/'+oid,headers={'Range':'bytes=1-5'})
        assert head.status_code==206 and head.headers['content-length']=='5'
        assert head.content==b''
        stale=await http.get(read+'/info/lfs/objects/'+oid,
                             headers={'Range':'bytes=1-5','If-Range':'"different"'})
        assert stale.status_code==200 and stale.content==data
        invalid=await http.get(read+'/info/lfs/objects/'+oid,
                               headers={'Range':f'bytes={len(data)}-'})
        assert invalid.status_code==416 and invalid.headers['content-range']==f'bytes */{len(data)}'
        missing_auth=await http.put(path,content=data)
        assert missing_auth.status_code==401
        archived=await call(app,'content.archive',{'id':rid},key=key,subject=user,
                            expected=((rid,created.data['generation']),))
        assert archived.status=='ok',wire(archived)
        denied=await http.get(read+'/info/lfs/objects/'+oid)
        assert denied.status_code!=200


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


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which('git-lfs') is None,reason='git-lfs CLI unavailable')
async def test_real_git_lfs_push_and_pull(installed,tmp_path):
    app,_=installed
    key,user,_=await register(app,'lfs-real')
    created=await call(app,'git.create',{'parent':'/@lfs-real','name':'code.git'},
                       key=key,subject=user)
    rid=created.resources[0].id
    issued=await call(app,'identity.token_create',{'nonce':b64(os.urandom(32)),
        'recovery_secret':b64(os.urandom(32)),
        'ceiling':wire((grant_for(app.registry.capability('git.basic')),)),'ttl':3600},
        key=key,subject=user,contract_version=2)
    basic='Basic '+base64.b64encode((issued.data['credential_id']+':'+issued.data['token']).encode()).decode()
    application=create_app(app)
    port_holder=[0]
    async def host_bridge(scope,receive,send):
        if scope['type']=='http':
            scope={**scope,'headers':[(name,b'testserver' if name==b'host' else value)
                                       for name,value in scope['headers']]}
        if scope['type']!='http' or not scope['path'].endswith('/info/lfs/objects/batch'):
            return await application(scope,receive,send)
        messages=[]
        async def collect(message):messages.append(message)
        await application(scope,receive,collect)
        source=b'http://testserver'
        destination=f'http://127.0.0.1:{port_holder[0]}'.encode()
        for message in messages:
            if message['type']=='http.response.body':
                message={**message,'body':message.get('body',b'').replace(source,destination)}
            elif message['type']=='http.response.start':
                message={**message,'headers':[(name,value) for name,value in message['headers']
                                            if name.lower()!=b'content-length']}
            await send(message)
    server=uvicorn.Server(uvicorn.Config(host_bridge,host='127.0.0.1',port=0,
                                         log_level='error',lifespan='off'))
    task=asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started and server.servers:break
            await asyncio.sleep(0.05)
        assert server.started and server.servers
        port_holder[0]=server.servers[0].sockets[0].getsockname()[1]
        work=tmp_path/'work';work.mkdir()
        env={**os.environ,'GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0'}
        def git(folder,*args):
            return subprocess.run(['git','-C',str(folder),*args],capture_output=True,text=True,
                                  timeout=90,env=env)
        assert git(work,'init','--initial-branch=main').returncode==0
        assert git(work,'config','user.name','Test').returncode==0
        assert git(work,'config','user.email','test@example.invalid').returncode==0
        assert git(work,'lfs','install','--local').returncode==0
        assert git(work,'lfs','track','*.bin').returncode==0
        body=os.urandom(1_300_000)
        (work/'payload.bin').write_bytes(body)
        assert git(work,'add','.').returncode==0
        assert git(work,'commit','-m','LFS file').returncode==0
        write_url=f'http://127.0.0.1:{port_holder[0]}/-/git/{rid}'
        pushed=await asyncio.to_thread(git,work,'-c','http.extraHeader=Authorization: '+basic,
                                       '-c','http.extraHeader=X-Msg-Request-Id: lfs-real-push',
                                       '-c','http.postBuffer=1048576','push',write_url,'main')
        assert pushed.returncode==0,pushed.stderr
        oid=hashlib.sha256(body).hexdigest()
        assert app.settings.server.repositories_dir.joinpath(rid+'.git','lfs','objects',
                                                             oid[:2],oid[2:4],oid).is_file()
        read_url=f'http://127.0.0.1:{port_holder[0]}/@lfs-real/code.git'
        clone=tmp_path/'clone'
        cloned=await asyncio.to_thread(subprocess.run,['git','-c','http.extraHeader=Authorization: '+basic,
            'clone',read_url,str(clone)],capture_output=True,text=True,timeout=90,env={**env,'GIT_LFS_SKIP_SMUDGE':'1'})
        assert cloned.returncode==0,cloned.stderr
        assert git(clone,'lfs','install','--local').returncode==0
        pulled=await asyncio.to_thread(git,clone,'-c','http.extraHeader=Authorization: '+basic,
                                       'lfs','pull')
        assert pulled.returncode==0,pulled.stderr
        assert (clone/'payload.bin').read_bytes()==body,(pulled.stdout,pulled.stderr,
            git(clone,'lfs','ls-files').stdout)
    finally:
        server.should_exit=True
        await task
