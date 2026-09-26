"""Native public Git repositories, physically separate from internal topic Git."""
from __future__ import annotations
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from starlette.responses import Response,StreamingResponse
from msg.core.codec import canonical,decode,wire
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef,EffectJob
from msg.core.requests import request_for
from msg.plugins.common import check_access,create_resource,resolve,output_for,new_id,operation_id
from msg.plugins.communication import event_id
from msg.plugins.schemas import obj,STRING,IDENTIFIER,REF,BOOLEAN
from msg.storage.git import durable_write

OID={'type':['string','null'],'pattern':'^[0-9a-f]{40}$'}
CHANGE=obj({'ref':{'type':'string','pattern':'^refs/(heads|tags)/[^\\s]+$'},'old':OID,'new':OID},('ref','old','new'))


class NativeGitStore:
    def __init__(self,app):
        self.app=app
        self.root=app.settings.server.content_dir.parent/'repositories'
        self.env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8','HOME':'/nonexistent',
            'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
            'GIT_COMMITTER_NAME':'msg','GIT_COMMITTER_EMAIL':'msg@localhost'}

    def path(self,id):
        require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',id)),'invalid_repository_id')
        return self.root/(id+'.git')

    def _run(self,id,*args,input=None,stdout=None):
        command=['git','--git-dir',str(self.path(id)),'-c','core.hooksPath=/dev/null',
            '-c','core.fsync=all','-c','protocol.file.allow=always',*args]
        result=subprocess.run(command,input=input,stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,env=self.env,timeout=120)
        require(result.returncode==0,'git_operation_failed')
        return result.stdout

    async def create(self,id):
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

    async def http(self,request,repo_path,suffix):
        require(suffix!='git-receive-pack','method_not_allowed')
        require(suffix in {'info/refs','git-upload-pack','HEAD'},'not_found')
        require((request.method in {'GET','HEAD'} and suffix in {'info/refs','HEAD'}) or
                (request.method=='POST' and suffix=='git-upload-pack'),'method_not_allowed')
        query=request.query_params
        require(not query or dict(query)=={'service':'git-upload-pack'},'git_push_requires_authenticated_transport')
        result=await self.app.executor.execute(request_for('git.refs',{'id':repo_path,'limit':1},self.app.settings.service_url))
        require(result.status=='ok',result.error.code if result.error else 'permission_denied')
        rid=result.data['resource_id']
        from msg.transports.http import body_bytes
        body=await body_bytes(request,self.app.settings.server.limits.max_request_bytes) if request.method=='POST' else b''
        env={**self.env,'GIT_PROJECT_ROOT':str(self.root),'PATH_INFO':'/'+rid+'.git/'+suffix,
            'REQUEST_METHOD':'GET' if request.method=='HEAD' else request.method,
            'QUERY_STRING':'service=git-upload-pack' if query else '',
            'CONTENT_TYPE':'application/x-git-upload-pack-request' if body else '', 'CONTENT_LENGTH':str(len(body))}
        protocol=request.headers.get('git-protocol')
        if protocol:
            require(protocol in {'version=0','version=1','version=2'},'invalid_git_protocol')
            env['GIT_PROTOCOL']=protocol
        process=await asyncio.create_subprocess_exec('git','http-backend',stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env,limit=65536)
        async def feed():
            try:
                process.stdin.write(body);await process.stdin.drain()
            finally:process.stdin.close()
        sender=asyncio.create_task(feed())
        headers={};size=0;status=200
        try:
            while True:
                line=await asyncio.wait_for(process.stdout.readline(),30)
                size+=len(line)
                require(line and size<=8192,'invalid_git_response')
                if line in {b'\r\n',b'\n'}:break
                key,_,value=line.decode('latin1').partition(':')
                if key.lower()=='status':status=int(value.strip().split(' ',1)[0])
                elif key.lower() in {'content-type','cache-control','expires','pragma'}:headers[key]=value.strip()
            if request.method=='HEAD':
                if process.returncode is None:process.kill()
                await process.wait();await sender
                return Response(status_code=status,headers=headers)
        except BaseException:
            if process.returncode is None:process.kill()
            await process.wait();sender.cancel()
            raise
        async def stream():
            try:
                while chunk:=await asyncio.wait_for(process.stdout.read(65536),120):yield chunk
                await sender
                require(await process.wait()==0,'git_operation_failed')
            finally:
                if process.returncode is None:process.kill();await process.wait()
                if not sender.done():sender.cancel()
        return StreamingResponse(stream(),status_code=status,headers={**headers,'X-Content-Type-Options':'nosniff'})


def register(app,op):
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
        return output_for(resource,path=await tx.path(resource.id),public=True)

    @op('git.refs',obj({'id':IDENTIFIER,'cursor':STRING,'limit':{'type':'integer','minimum':1,'maximum':200}},('id',)),effect='read')
    async def refs(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        resource=await tx.resource(rid)
        require(resource.type=='repo' and resource.state=='active','not_a_repository')
        after=app.cursors.decode(request.arguments['cursor'],'git.refs',rid) if request.arguments.get('cursor') else ''
        rows,following=await NativeGitStore(app).refs(rid,after=after,limit=request.arguments.get('limit',100))
        return HandlerOutput(data={'resource_id':rid,'refs':rows,
            'cursor':app.cursors.encode('git.refs',rid,following) if following else None})

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
            operation=request.operation,arguments={'id':rid,'bundle':wire(source),'changes':request.arguments['changes'],
                'force':request.arguments.get('force',False),'request_id':request.request_id},state='pending',attempts=0,
            next_attempt_at=ctx.now,lease_until=None)
        await tx.enqueue(job)
        return HandlerOutput(resources=(ResourceRef(id=rid),),data={'job_id':job.id,'state':'pending'})


async def execute_push(app,job):
    from msg.workers.effects import current_principal,worker_context,effect_request
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
        await store.import_bundle(job.arguments['id'],bundle,job.arguments['changes'],job.arguments.get('force',False))
        moved=False
        try:
            async with app.metadata.transaction(write=True) as tx:
                principal=await current_principal(app,job.principal,tx)
                context,request=worker_context(app,job,principal),effect_request(app,job,principal)
                await check_access(app,context,request,tx,job.arguments['id'],'write')
                require(tx.setting('runtime_config',{}).get('accept_writes',True),'writes_paused')
                current=await tx.job(job.id)
                require(current.state=='running' and current.attempts==job.attempts,'job_lease_lost')
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
