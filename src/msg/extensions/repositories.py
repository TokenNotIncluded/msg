"""Native public Git repositories, physically separate from internal topic Git."""
from __future__ import annotations
import asyncio
import base64
from contextvars import ContextVar
from dataclasses import replace
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4
from urllib.parse import urlsplit

from msg.core.codec import canonical,decode,wire,unb64,b64
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef,EffectJob,ExecutionContext
from msg.core.requests import request_for
from msg.plugins.common import check_access,create_resource,resolve,output_for,new_id,operation_id
from msg.plugins.communication import event_id
from msg.plugins.schemas import obj,STRING,IDENTIFIER,REF,BOOLEAN
from msg.storage.git import durable_write,LFSObjectStore

OID={'type':['string','null'],'pattern':'^[0-9a-f]{40}$'}
CHANGE=obj({'ref':{'type':'string','pattern':'^refs/(heads|tags)/[^\\s]+$'},'old':OID,'new':OID},('ref','old','new'))
_HTTP_RECEIVE=ContextVar('msg_git_http_receive',default=False)
# A separate, deliberately bounded Git pack budget. Two active uploads can use
# at most 64 MiB of staging space per worker; other protocol requests retain
# the configured (normally 1 MiB) request limit.
MAX_GIT_PACK_BYTES=32*1024*1024
MAX_GIT_UPLOAD_SECONDS=120
MAX_GIT_REPOSITORY_BYTES=2*1024*1024*1024
MAX_LFS_OBJECT_BYTES=256*1024*1024
# Deployment-wide, not per-account. Operators may lower this for their shared
# volume; PostgreSQL records the value so workers with divergent settings fail.
DEFAULT_LFS_DEPLOYMENT_BYTES=4*1024*1024*1024
_LFS_STAGED=ContextVar('msg_lfs_staged',default=None)


def git_repository_bytes(root):
    """Byte size of regular files under root, without following symlinks."""
    total=0
    if not Path(root).is_dir():
        return 0
    for directory,_dirnames,names in os.walk(root,followlinks=False):
        for name in names:
            try:
                total+=(Path(directory)/name).lstat().st_size
            except OSError:
                continue
            if total>MAX_GIT_REPOSITORY_BYTES:
                return total
    return total


def require_git_repository_capacity(root,incoming=MAX_GIT_PACK_BYTES):
    require(type(incoming) is int and incoming>=0,'storage_capacity_exceeded')
    require(git_repository_bytes(root)+incoming<=MAX_GIT_REPOSITORY_BYTES,'storage_capacity_exceeded')


class NativeGitStore:
    def __init__(self,app):
        self.blob_root=app.settings.server.blob_dir
        self.root=app.settings.server.repositories_dir
        self.env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8','HOME':'/nonexistent',
            'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
            'GIT_COMMITTER_NAME':'msg','GIT_COMMITTER_EMAIL':'msg@localhost'}

    def path(self,id):
        require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',id)),'invalid_repository_id')
        return self.root/(id+'.git')

    def lfs(self,id):
        return LFSObjectStore(self.path(id),self.blob_root)

    def lfs_capacity(self,tx):
        configured=os.environ.get('MSG_LFS_DEPLOYMENT_MAX_BYTES',str(DEFAULT_LFS_DEPLOYMENT_BYTES))
        require(configured.isdecimal() and int(configured)>0,'invalid_lfs_deployment_capacity')
        limit=int(configured)
        previous=tx.setting('lfs_deployment_capacity_bytes')
        require(previous is None or previous==limit,'lfs_deployment_capacity_mismatch')
        for root,key in ((self.blob_root,'lfs_shared_store_id'),
                         (self.root,'lfs_shared_repositories_id')):
            root.mkdir(parents=True,exist_ok=True)
            sentinel=root/'.msg-shared-store-id'
            known=tx.setting(key)
            if not sentinel.exists():
                require(known is None,'lfs_shared_volume_required')
                durable_write(sentinel,(uuid4().hex+'\n').encode())
            identity=sentinel.read_text().strip()
            require(bool(re.fullmatch(r'[0-9a-f]{32}',identity)),'invalid_lfs_shared_store_id')
            require(known is None or known==identity,'lfs_shared_volume_required')
            if known is None:tx.set_setting(key,identity)
        if previous is None:tx.set_setting('lfs_deployment_capacity_bytes',limit)
        return limit


    def _run(self,id,*args,input=None,stdout=None):
        command=['git','--git-dir',str(self.path(id)),'-c','core.hooksPath=/dev/null',
            '-c','core.fsync=all','-c','protocol.file.allow=always',*args]
        result=subprocess.run(command,input=input,stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,env=self.env,timeout=120)
        require(result.returncode==0,'git_operation_failed')
        return result.stdout

    async def create(self,id, *, public_export=True):
        path=self.path(id)
        self.root.mkdir(parents=True,exist_ok=True)
        require(not path.exists(),'repository_directory_exists')
        temporary=Path(tempfile.mkdtemp(prefix='.pending-',dir=self.root))
        try:
            result=await asyncio.to_thread(subprocess.run,['git','init','--bare','--initial-branch=main',str(temporary)],
                capture_output=True,env=self.env,timeout=30)
            require(result.returncode==0,'git_initialization_failed')
            # Disable access to unreferenced objects and all implicit remote writes.
            for key,value in {'http.receivepack':'false','http.getanyfile':'false',
                'uploadpack.allowAnySHA1InWant':'false','uploadpack.allowReachableSHA1InWant':'false',
                'uploadpack.allowTipSHA1InWant':'false','receive.fsckObjects':'true',
                'transfer.fsckObjects':'true','core.hooksPath':'/dev/null','core.fsync':'all'}.items():
                result=await asyncio.to_thread(subprocess.run,['git','--git-dir',str(temporary),'config',key,value],
                    capture_output=True,env=self.env,timeout=15)
                require(result.returncode==0,'git_initialization_failed')
            if public_export:
                durable_write(temporary/'git-daemon-export-ok',b'public resource\n',mode=0o644)
            os.rename(temporary,path)
            fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            if temporary.exists():shutil.rmtree(temporary)

    async def refs(self,id, *, after='',limit=100):
        # Bounded stream: do not put the complete ref set into server memory.
        process=await asyncio.create_subprocess_exec('git','--git-dir',str(self.path(id)),
            'for-each-ref','--format=%(refname) %(objectname)','--sort=refname','refs/heads','refs/tags',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=self.env)
        items=[];more=False
        try:
            while line:=await process.stdout.readline():
                name,oid=line.decode('utf-8').rstrip('\n').split(' ',1)
                if name<=after:continue
                if len(items)>=limit:more=True;break
                items.append({'name':name,'oid':oid})
            if not more:require(await process.wait()==0,'git_operation_failed')
        finally:
            if process.returncode is None:process.kill();await process.wait()
        return items,items[-1]['name'] if more else None

    async def import_bundle(self,id,bundle_path,changes,force=False):
        require(self.path(id).is_dir(),'repository_missing')
        # bundle unbundle imports object data only. No refs move yet; no commands,
        # hooks, repository config or external remote URL come from the bundle.
        await asyncio.to_thread(self._run,id,'bundle','verify',str(bundle_path))
        await asyncio.to_thread(self._run,id,'bundle','unbundle',str(bundle_path))
        await asyncio.to_thread(self._run,id,'fsck','--no-reflogs','--connectivity-only')
        for change in changes:
            name=change['ref']
            require(re.fullmatch(r'refs/(heads|tags)/[^\s]+',name) is not None,'invalid_git_ref')
            await asyncio.to_thread(self._run,id,'check-ref-format',name)
            old,new=change['old'],change['new']
            if new:
                # Branch tips must be commits; tags may reference another object.
                suffix='^{commit}' if name.startswith('refs/heads/') else '^{object}'
                await asyncio.to_thread(self._run,id,'rev-parse','--verify',new+suffix)
            if old and new and not force and name.startswith('refs/heads/'):
                await asyncio.to_thread(self._run,id,'merge-base','--is-ancestor',old,new)
        return changes

    async def update_refs(self,id,changes):
        lines=['start']
        zero='0'*40
        for change in changes:
            if change['new'] is None:
                require(change['old'] is not None,'invalid_git_change')
                lines.append('delete '+change['ref']+' '+change['old'])
            else:
                lines.append('update '+change['ref']+' '+change['new']+' '+(change['old'] or zero))
        lines+=['prepare','commit','']
        await asyncio.to_thread(self._run,id,'update-ref','--stdin',input='\n'.join(lines).encode())


def register(app,op):
    @op('git.lfs_read_batch',obj({'id':IDENTIFIER},('id',)),effect='read')
    async def lfs_read_batch(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        return HandlerOutput(data={'resource_id':rid})

    @op('git.lfs_read',obj({'id':IDENTIFIER,'oid':{'type':'string','pattern':'^[0-9a-f]{64}$'}},
        ('id','oid')),effect='read')
    async def lfs_read(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        return HandlerOutput(data={'resource_id':rid})

    @op('git.lfs_write_authorize',obj({'id':IDENTIFIER},('id',)),effect='read')
    async def lfs_write_authorize(ctx,request,tx):
        require(ctx.principal.method in {'signature','token'},'authentication_required')
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'write')
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        return HandlerOutput(data={'resource_id':rid})

    @op('git.lfs_publish',obj({'id':IDENTIFIER,'oid':{'type':'string','pattern':'^[0-9a-f]{64}$'},
        'size':{'type':'integer','minimum':0,'maximum':MAX_LFS_OBJECT_BYTES}},('id','oid','size')))
    async def lfs_publish(ctx,request,tx):
        staged=_LFS_STAGED.get()
        require(staged is not None and staged.is_file(),'lfs_transport_required')
        require(ctx.principal.method in {'signature','token'},'authentication_required')
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'write')
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        store=NativeGitStore(app)
        limit=store.lfs_capacity(tx)
        await asyncio.to_thread(store.lfs(rid).publish,request.arguments['oid'],
                                staged,request.arguments['size'],quota_bytes=limit)
        return HandlerOutput(data={'resource_id':rid,'oid':request.arguments['oid'],
                                   'size':request.arguments['size']})

    @op('git.lfs_migrate',obj({'id':IDENTIFIER,
        'oid':{'type':'string','pattern':'^[0-9a-f]{64}$'}},('id','oid')))
    async def lfs_migrate(ctx,request,tx):
        require(ctx.principal.method in {'signature','token'},'authentication_required')
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'write')
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        store=NativeGitStore(app)
        limit=store.lfs_capacity(tx)
        result=await asyncio.to_thread(store.lfs(rid).migrate_legacy,
            request.arguments['oid'],quota_bytes=limit)
        return HandlerOutput(data={'resource_id':rid,'oid':request.arguments['oid'],**result})

    @op('git.create',obj({'parent':IDENTIFIER,'name':STRING},('parent','name')),signature=True)
    async def create(ctx,request,tx):
        parent=await tx.resource(await resolve(tx,request.arguments['parent']))
        require(parent.type in {'user','organization'},'repository_namespace_required')
        name=request.arguments['name']
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,90}\.git',name) is not None,'invalid_repository_name')
        await check_access(app,ctx,request,tx,parent.id,'create')
        require(parent.mode&1 and all(r.mode&1 for r in await tx.ancestors(parent.id)),'repo_public_read_required')
        resource=await create_resource(app,ctx,request,tx,parent=parent.id,type='repo',name=name,
            mode=0o664 if parent.type=='organization' else 0o644)
        await NativeGitStore(app).create(resource.id)
        path=await tx.path(resource.id)
        return output_for(resource,path=path,public=True,
            read_url=app.settings.service_url.rstrip('/')+path,
            push_url=app.settings.service_url.rstrip('/')+'/-/git/'+resource.id,
            push_max_bytes=MAX_GIT_PACK_BYTES)

    @op('git.refs',obj({'id':IDENTIFIER,'cursor':STRING,'limit':{'type':'integer','minimum':1,'maximum':200}},('id',)),effect='read')
    async def refs(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        after=app.cursors.decode(request.arguments['cursor'],'git.refs',rid) if request.arguments.get('cursor') else ''
        rows,following=await NativeGitStore(app).refs(rid,after=after,limit=request.arguments.get('limit',100))
        return HandlerOutput(data={'resource_id':rid,'refs':rows,
            'read_url':app.settings.service_url.rstrip('/')+await tx.path(rid),
            'push_url':app.settings.service_url.rstrip('/')+'/-/git/'+rid,
            'push_max_bytes':MAX_GIT_PACK_BYTES,
            'cursor':app.cursors.encode('git.refs',rid,following) if following else None})

    @op('git.http_advertise',obj({'id':IDENTIFIER},('id',)),effect='read')
    async def http_advertise(ctx,request,tx):
        require(ctx.principal.method in {'signature','token'},'authentication_required')
        rid=await resolve(tx,request.arguments['id'])
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        await check_access(app,ctx,request,tx,rid,'write')
        return HandlerOutput(data={'resource_id':rid})

    @op('git.http_receive',obj({'id':IDENTIFIER,'pack_digest':STRING,
        'pack_size':{'type':'integer','minimum':0}},('id','pack_digest','pack_size')),effect='external')
    async def http_receive(ctx,request,tx):
        require(_HTTP_RECEIVE.get(),'git_http_transport_required')
        require(ctx.principal.method in {'signature','token'},'authentication_required')
        rid=await resolve(tx,request.arguments['id'])
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        await check_access(app,ctx,request,tx,rid,'write')
        require(request.arguments['pack_digest'].startswith('sha256:') and
                len(request.arguments['pack_digest'])==71,'invalid_pack_digest')
        id,eid=new_id('job'),event_id(request,ctx.principal.subject)
        job=EffectJob(id=id,event_id=eid,kind='git.receive',dedupe_key='git-http:'+eid,
            principal=ctx.principal,operation=request.operation,
            arguments={'contract_version':request.contract_version,'id':rid,'request_id':request.request_id},state='running',attempts=1,
            next_attempt_at=ctx.now,lease_until=ctx.now+timedelta(seconds=600))
        await tx.enqueue(job)
        return HandlerOutput(resources=(ResourceRef(id=rid),),data={'job_id':id})

    @op('git.push',obj({'id':IDENTIFIER,'bundle':REF,'changes':{'type':'array','items':CHANGE,'minItems':1,'maxItems':128},
        'force':BOOLEAN},('id','bundle','changes')),effect='external',signature=True)
    async def push(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        require((await tx.resource(rid)).type=='repo','not_a_repository')
        await check_access(app,ctx,request,tx,rid,'write')
        source=decode(ResourceRef,request.arguments['bundle']);require(source.revision is not None,'source_revision_required')
        await check_access(app,ctx,request,tx,source.id,'read');await tx.revision(source)
        require(len({v['ref'] for v in request.arguments['changes']})==len(request.arguments['changes']),'duplicate_git_ref')
        eid=event_id(request,ctx.principal.subject)
        job=EffectJob(id=new_id('job'),event_id=eid,kind='git.push',dedupe_key='git:'+eid,principal=ctx.principal,
            operation=request.operation,arguments={'contract_version':request.contract_version,'id':rid,'bundle':wire(source),'changes':request.arguments['changes'],
                'force':request.arguments.get('force',False),'request_id':request.request_id},state='pending',attempts=0,
            next_attempt_at=ctx.now,lease_until=None)
        await tx.enqueue(job)
        return HandlerOutput(resources=(ResourceRef(id=rid),),data={'job_id':job.id,'state':'pending'})


async def execute_push(app,job):
    from msg.workers.effects import current_principal,worker_context,effect_request
    from msg.workers.leases import current_attempt
    store=NativeGitStore(app)
    app.settings.server.staging_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='git-push-',dir=app.settings.server.staging_dir) as temp:
        bundle=Path(temp)/'input.bundle'
        async with app.metadata.transaction(write=False) as tx:
            principal=await current_principal(app,job.principal,tx)
            context,request=worker_context(app,job,principal),effect_request(app,job,principal)
            await check_access(app,context,request,tx,job.arguments['id'],'write')
            ref=decode(ResourceRef,job.arguments['bundle'])
            await check_access(app,context,request,tx,ref.id,'read')
            rev=await tx.revision(ref)
            with bundle.open('xb') as output:
                async for chunk in app.contents.read(rev.content):output.write(chunk)
        require_git_repository_capacity(store.path(job.arguments['id']),incoming=bundle.stat().st_size)
        await store.import_bundle(job.arguments['id'],bundle,job.arguments['changes'],job.arguments.get('force',False))
        moved=False
        try:
            async with app.metadata.transaction(write=True) as tx:
                principal=await current_principal(app,job.principal,tx)
                context,request=worker_context(app,job,principal),effect_request(app,job,principal)
                await check_access(app,context,request,tx,job.arguments['id'],'write')
                require(tx.setting('runtime_config',{}).get('accept_writes',True),'writes_paused')
                current=await current_attempt(app,tx,job)
                if current is None:
                    return
                await store.update_refs(job.arguments['id'],job.arguments['changes'])
                moved=True
                resource=await tx.resource(job.arguments['id'])
                await tx.replace(replace(resource,generation=resource.generation+1,modified_at=app.clock(),
                    modified_by=principal.actor),resource.generation)
                await tx.save_job(replace(current,state='done',lease_until=None))
                tx.set_setting('job_status:'+job.id,{'code':'ok','refs':job.arguments['changes']})
        except BaseException as exc:
            if moved:raise Failure('external_uncertain') from exc
            raise
