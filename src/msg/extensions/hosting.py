"""Explicit static publishing on a separate origin, never a platform HTML UI."""
from __future__ import annotations
from dataclasses import replace
from pathlib import PurePosixPath
from urllib.parse import urlsplit,unquote,quote
import re
from starlette.applications import Starlette
from starlette.responses import Response,StreamingResponse
from starlette.routing import Route
from msg.constants import ROOT_SPACE
from msg.core.codec import canonical,decode,loads,wire,digest
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef
from msg.core.requests import request_for
from msg.plugins.common import check_access,create_resource,revise_resource,resolve,output_for,assert_generation,new_id
from msg.plugins.schemas import obj,STRING,IDENTIFIER,REF


def path_name(value):
    require(isinstance(value,str) and 0<len(value)<=2048 and not value.startswith('/') and '\\' not in value and
        all(ord(c)>=32 and ord(c)!=127 for c in value),'invalid_hosting_path')
    parts=value.split('/')
    require(all(part not in {'','.','..'} and not part.startswith('.') for part in parts),'invalid_hosting_path')
    return '/'.join(parts)


def register(app,op):
    @op('hosting.create',obj({'parent':IDENTIFIER,'name':STRING},('parent','name')),signature=True)
    async def create(ctx,request,tx):
        require(app.settings.public_web_origin is not None,'hosting_origin_not_configured')
        parent=await resolve(tx,request.arguments['parent'])
        require((await tx.resource(parent)).type in {'user','organization'},'hosting_namespace_required')
        await check_access(app,ctx,request,tx,parent,'create')
        require(all(r.mode&1 for r in [await tx.resource(parent),*await tx.ancestors(parent)]),'hosting_public_path_required')
        resource=await create_resource(app,ctx,request,tx,parent=parent,type='website',name=request.arguments['name'],mode=0o755)
        return output_for(resource,url=app.settings.public_web_origin+await tx.path(resource.id)+'/')

    @op('hosting.deploy',obj({'id':IDENTIFIER,'entries':{'type':'array','minItems':1,
        'items':obj({'path':STRING,'source':REF},('path','source'))}},('id','entries')),signature=True)
    async def deploy(ctx,request,tx):
        require(app.settings.public_web_origin is not None,'hosting_origin_not_configured')
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        require(resource.type=='website' and resource.state=='active','not_a_website')
        await check_access(app,ctx,request,tx,resource.id,'write')
        await assert_generation(request,resource)
        sources=[];names=set()
        for item in request.arguments['entries']:
            path=path_name(item['path'])
            require(path not in names,'duplicate_hosting_path');names.add(path)
            ref=decode(ResourceRef,item['source']);require(ref.revision is not None,'source_revision_required')
            await check_access(app,ctx,request,tx,ref.id,'read')
            revision=await tx.revision(ref)
            sources.append((path,ref,revision.content))
        # Public copies share CAS bytes but get their own explicit publication
        # facts and ACL. A later source chmod does not secretly unpublish a site.
        deployment=await create_resource(app,ctx,request,tx,parent=resource.id,type='topic',
            name='deploy-'+new_id('d'),mode=0o755)
        entries={}
        for index,(path,source,blob) in enumerate(sources):
            published=await create_resource(app,ctx,request,tx,parent=deployment.id,type='file',name=f'file-{index}',
                body=blob,media_type=blob.media_type,mode=0o644)
            tx.set_setting('publication:'+published.id,{'source':wire(source),'website':resource.id,
                'path':path,'publisher':ctx.principal.subject})
            entries[path]={'id':published.id,'revision':published.revision}
        body=canonical({'format_version':1,'deployment':deployment.id,'entries':entries})
        resource=await revise_resource(app,ctx,request,tx,resource,body,'application/json')
        return output_for(resource,files=len(entries),url=app.settings.public_web_origin+await tx.path(resource.id)+'/')

    @op('hosting.activate',obj({'id':IDENTIFIER,'revision':IDENTIFIER},('id','revision')),signature=True)
    async def activate(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        require(resource.type=='website','not_a_website')
        await check_access(app,ctx,request,tx,resource.id,'write')
        await assert_generation(request,resource)
        revision=await tx.revision(ResourceRef(id=resource.id,revision=request.arguments['revision']))
        # Validation binds the deployment to this website, not an arbitrary JSON file.
        value=loads(await app.contents.read_bytes(revision.content))
        require((await tx.resource(value['deployment'])).parent==resource.id,'deployment_mismatch')
        changed=replace(resource,revision=revision.id,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(changed,resource.generation)
        return output_for(changed)


def hosting_app(service):
    async def dispatch(request):
        try:
            require(service.settings.public_web_origin is not None,'hosting_origin_not_configured')
            expected=urlsplit(service.settings.public_web_origin)
            supplied=urlsplit('//'+request.headers.get('host',''))
            require(supplied.hostname==expected.hostname,'forbidden_host')
            require(request.method in {'GET','HEAD'},'method_not_allowed')
            parts=request.url.path.strip('/').split('/')
            require(len(parts)>=2 and parts[0].startswith(('@','&')),'not_found')
            site_path='/'+parts[0]+'/'+parts[1]
            file_path='/'.join(parts[2:]) or 'index.html'
            if request.url.path.endswith('/') and len(parts)>2:file_path+='/index.html'
            file_path=path_name(file_path)
            async with service.metadata.transaction(write=False) as tx:
                rid=await tx.resolve(site_path)
                website=await tx.resource(rid)
                require(website.type=='website' and website.state=='active' and website.revision is not None,'not_found')
                packet=request_for('discovery.raw',{'id':rid},service.settings.service_url)
                principal=await service.authenticator.authenticate(packet,tx,entry='network')
                from msg.core.models import ExecutionContext
                import time
                context=ExecutionContext(request_id=packet.request_id,principal=principal,entry='network',now=service.clock(),
                                         deadline_monotonic=time.monotonic()+30)
                await check_access(service,context,packet,tx,rid,'read')
                revision=await tx.revision(ResourceRef(id=rid))
                manifest=loads(await service.contents.read_bytes(revision.content))
                item=manifest['entries'].get(file_path)
                require(item is not None,'not_found')
                ref=decode(ResourceRef,item)
                await check_access(service,context,packet,tx,ref.id,'read')
                blob=(await tx.revision(ref)).content
            headers={'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                'Cross-Origin-Resource-Policy':'same-origin','ETag':'"'+blob.digest+'"',
                'Content-Length':str(blob.size),'Cache-Control':'public, max-age=0, must-revalidate'}
            if blob.media_type.split(';',1)[0].strip().lower()=='text/html':
                # Hosted HTML must never inherit the service origin, including
                # on HEAD and conditional responses.
                headers['Content-Security-Policy']="sandbox allow-scripts; default-src 'none'"
            if request.headers.get('if-none-match')==headers['ETag']:return Response(status_code=304,headers=headers)
            if request.method=='HEAD':return Response(media_type=blob.media_type,headers=headers)
            return StreamingResponse(service.contents.read(blob),media_type=blob.media_type,headers=headers)
        except Failure as exc:
            code=403 if exc.code in {'forbidden_host','permission_denied'} else 405 if exc.code=='method_not_allowed' else 404
            return Response(canonical({'error':exc.code}),status_code=code,media_type='application/json',headers={'Cache-Control':'no-store'})
    return Starlette(routes=[Route('/{path:path}',dispatch,methods=['GET','HEAD','POST','PUT','DELETE'])])
