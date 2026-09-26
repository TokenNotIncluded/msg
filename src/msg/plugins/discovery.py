"""ACL-filtered reads and rebuildable discovery projections."""
from __future__ import annotations
import difflib
from msg.constants import *
from msg.core.codec import canonical,wire,decode,loads,digest,b64
from msg.core.errors import Failure,require
from msg.core.models import Resource,ResourceRef,HandlerOutput,Credential
from msg.core.requests import request_for
from msg.core.template_dsl import render_values
from msg.plugins.common import *
from msg.plugins.schemas import *


async def visible(app,ctx,request,tx,rid):
    try:
        await check_access(app,ctx,request,tx,rid,'read')
        return True
    except Failure as exc:
        if exc.code in {'permission_denied','credential_ceiling','certificate_gate','tool_certificate_required',
                        'delegation_scope','ancestor_inactive'}:
            return False
        raise


async def metadata(tx,r):
    data=wire(r)
    data['path']=await tx.path(r.id)
    if r.revision:
        rev=await tx.revision(ResourceRef(id=r.id))
        data.update(size=rev.content.size,media_type=rev.content.media_type,digest=rev.content.digest)
    return data


async def filtered_tools(app,ctx,request,tx):
    result=[]
    page=await tx.children(TOOLS_SPACE,limit=500)
    for resource in page.items:
        if await app.authorizer.has(ctx.principal,'tool.use',operation_id(request),resource.id,tx):
            result.append({'id':resource.id,'name':resource.name,'path':await tx.path(resource.id),'revision':resource.revision})
    require(bool(result),'tool_certificate_required')
    return result


async def read_projection(app,ctx,request,tx,rid, *, revision=None,fields=()):
    resource=await tx.resource(rid)
    if rid==TOOLS_SPACE:
        return {'id':rid,'type':'topic','items':await filtered_tools(app,ctx,request,tx)}
    await check_access(app,ctx,request,tx,rid,'read')
    require(resource.state!='purged','resource_purged')
    meta=await metadata(tx,resource)
    if rid=='t_capabilities':
        specs=[wire(s,compact=True) for s in app.registry.capabilities()]
        return {'version':1,'digest':digest(specs),'capabilities':specs}
    if rid=='t_operations':
        return app.registry.catalog()
    if resource.type=='certificate':
        return {'metadata':meta,'certificate':wire(await tx.certificate(rid)),'revoked':await tx.certificate_revoked(rid)}
    if resource.type=='csr':
        return {'metadata':meta,'request':wire(await tx.csr(rid)),'state':wire(await tx.csr_state(rid))}
    if resource.type=='user':
        subject=await tx.subject(rid)
        meta.update(kind=subject.kind,local_only=subject.local_only)
    if resource.name=='keys' and resource.parent is not None and (await tx.resource(resource.parent)).type=='user':
        keys=[]
        for row in tx.rows('SELECT body FROM credentials WHERE subject=?',(resource.parent,)):
            credential=decode(Credential,loads(row[0]))
            if credential.kind!='token':
                keys.append({'key_id':credential.id,'kind':credential.kind,'public_key':b64(credential.verifier),
                             'revoked':credential.revoked_at is not None})
        return {'id':rid,'keys':keys}
    if resource.name=='certificates' and resource.parent is not None and (await tx.resource(resource.parent)).type=='user':
        return {'id':rid,'certificates':[{'id':row[0],'revoked':bool(row[1])} for row in
            tx.rows('SELECT id,revoked FROM certificates WHERE subject=? ORDER BY id',(resource.parent,))]}
    if app.registry.resource_type(resource.type,1).container and resource.type not in {'repo','website'}:
        values=[]
        cursor=None
        while len(values)<50:
            page=await tx.children(rid,cursor,50)
            for child in page.items:
                if child.state=='active' and await visible(app,ctx,request,tx,child.id):
                    values.append({'id':child.id,'name':child.name,'type':child.type,'revision':child.revision,
                                   'path':await tx.path(child.id)})
                    if len(values)==50:
                        break
            cursor=page.next_cursor
            if cursor is None:
                break
        meta['items']=values
        meta['list_operation']=next_link(app,'discovery.list',{'parent':rid})
    elif resource.revision:
        rev=await tx.revision(ResourceRef(id=rid,revision=revision))
        meta.update(revision=rev.id,digest=rev.content.digest,size=rev.content.size,media_type=rev.content.media_type)
        textual=rev.content.media_type.startswith('text/') or rev.content.media_type in {'application/json','application/msg-template'}
        if textual and rev.content.size<app.settings.server.limits.max_response_bytes//2:
            raw=await app.contents.read_bytes(rev.content,limit=app.settings.server.limits.max_response_bytes)
            meta['content']=loads(raw) if rev.content.media_type=='application/json' else raw.decode('utf-8')
        else:
            meta['raw_url']=f'/_id/{rid}/revisions/{rev.id}/raw'
            meta['transfer_operation']='transfer.open'
        meta['relations']=wire(rev.relations,compact=True)
    known=set(meta)
    if fields:
        require(set(fields)<=known,'unknown_projection_field')
        return {k:meta[k] for k in fields}
    defaults=('id','type','name','revision','generation','path','content','items','keys','certificates',
              'relations','raw_url','transfer_operation','kind','local_only','list_operation')
    return {k:meta[k] for k in defaults if k in meta}


def next_link(app,operation,args):
    packet=request_for(operation,args,app.settings.service_url,source='manual')
    return f'/~{operation}/run/j/'+b64(canonical(packet))


def install(app):
    op,finish=registration(app,'discovery',('identity','content'))
    fields={'type':'array','items':STRING,'maxItems':30,'uniqueItems':True}

    @op('discovery.get',obj({'id':IDENTIFIER,'revision':IDENTIFIER,'fields':fields,
        'view':{'enum':['json','meta','history']},'known_digest':STRING,'cursor':STRING,'limit':{'type':'integer','minimum':1,'maximum':200}},('id',)),effect='read')
    async def get(ctx,request,tx):
        a=request.arguments
        rid=await resolve(tx,a['id'])
        resource=await tx.resource(rid)
        if a.get('view') in {'meta','history'}:
            await check_access(app,ctx,request,tx,rid,'read')
            if a['view']=='meta':
                data=await metadata(tx,resource)
            else:
                cursor=app.cursors.decode(a['cursor'],'history',rid) if a.get('cursor') else None
                page=await tx.history(rid,cursor=cursor,limit=a.get('limit',50))
                data={'id':rid,'revisions':[{'id':v.id,'parents':list(v.parents),'digest':v.manifest_digest,
                    'created_at':wire(v.created_at),'actor':v.actor} for v in page.items]}
                if page.next_cursor:
                    cursor=app.cursors.encode('history',rid,page.next_cursor)
                    data.update(cursor=cursor,next=next_link(app,'discovery.get',{**a,'cursor':cursor}),
                                next_requires_auth=ctx.principal.subject is not None)
        else:
            data=await read_projection(app,ctx,request,tx,rid,revision=a.get('revision'),fields=a.get('fields',()))
        version=digest(data)
        if a.get('known_digest')==version:
            return HandlerOutput(data={'not_modified':True,'digest':version})
        return HandlerOutput(data=data)

    listing={'parent':IDENTIFIER,'type':STRING,'author':IDENTIFIER,'query':STRING,'state':{'enum':['active','archived','purged']},
             'sort':{'enum':['id','time','name']},'direction':{'enum':['asc','desc']},'limit':{'type':'integer','minimum':1,'maximum':200},'cursor':STRING,'fields':fields}
    async def list_items(ctx,request,tx):
        a=dict(request.arguments)
        parent=await resolve(tx,a['parent']) if a.get('parent') else None
        if parent==TOOLS_SPACE:
            return HandlerOutput(data={'items':await filtered_tools(app,ctx,request,tx)})
        if parent:
            await check_access(app,ctx,request,tx,parent,'list')
        limit=a.get('limit',50)
        query_hash=digest({k:v for k,v in a.items() if k!='cursor'})
        sort=a.get('sort','id')
        column={'id':'r.id','time':'r.created_at','name':'r.name'}[sort]
        descending=a.get('direction','asc')=='desc'
        comparison,ordering=('<','DESC') if descending else ('>','ASC')
        position=app.cursors.decode(a['cursor'],'page',query_hash) if a.get('cursor') else (['\uffff','\uffff'] if descending else ['', ''])
        filters=['r.state=?']
        parameters=[a.get('state','active')]
        if parent:
            filters.append('r.parent=?'); parameters.append(parent)
        if a.get('type'):
            app.registry.resource_type(a['type'],1)
            filters.append('r.type=?'); parameters.append(a['type'])
        if a.get('author'):
            filters.append('r.owner=?'); parameters.append(await resolve(tx,a['author']))
        if a.get('query'):
            text=a['query'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            filters.append("(r.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM projections p WHERE p.resource_id=r.id AND p.text LIKE ? ESCAPE '\\'))")
            parameters.extend(['%'+text+'%','%'+text+'%'])
        values=[]
        last_position=position
        more=False
        while len(values)<=limit:
            sql=f"SELECT r.body,{column},r.id FROM resources r WHERE {' AND '.join(filters)} AND ({column},r.id){comparison}(?,?) ORDER BY {column} {ordering},r.id {ordering} LIMIT 128"
            rows=tx.rows(sql,(*parameters,*last_position))
            if not rows:
                break
            for raw,order,rid in rows:
                last_position=[order,rid]
                resource=decode(Resource,loads(raw))
                if await visible(app,ctx,request,tx,rid):
                    if len(values)==limit:
                        more=True
                        break
                    data=await metadata(tx,resource)
                    chosen=a.get('fields',('id','type','name','revision','generation','path'))
                    require(set(chosen)<=set(data),'unknown_projection_field')
                    values.append({k:data[k] for k in chosen})
                    position=last_position
            if more or len(rows)<128:
                break
        data={'items':values}
        if more:
            cursor=app.cursors.encode('page',query_hash,position)
            data.update(cursor=cursor,next=next_link(app,request.operation,{**a,'cursor':cursor}),
                        next_requires_auth=ctx.principal.subject is not None)
        return HandlerOutput(data=data)
    op('discovery.list',obj(listing),effect='read')(list_items)
    op('discovery.search',obj(listing,('query',)),effect='read')(list_items)

    @op('discovery.operations',obj({'known_digest':STRING}),effect='read')
    async def operations(ctx,request,tx):
        data=app.registry.catalog()
        if request.arguments.get('known_digest')==data['digest']:
            data={'not_modified':True,'digest':data['digest']}
        return HandlerOutput(data=data)

    @op('discovery.capabilities',obj({'known_digest':STRING}),effect='read')
    async def capabilities(ctx,request,tx):
        values=[wire(s,compact=True) for s in app.registry.capabilities()]
        hashed=digest(values)
        return HandlerOutput(data={'not_modified':True,'digest':hashed} if request.arguments.get('known_digest')==hashed
            else {'version':1,'digest':hashed,'capabilities':values})

    @op('discovery.schema',obj({'operation':STRING},('operation',)),effect='read')
    async def schema(ctx,request,tx):
        spec=app.registry.operation(request.arguments['operation'])
        require('network' in spec.entries,'entry_not_allowed')
        return HandlerOutput(data={'operation':app.registry.describe(spec),'input':app.registry.schema(spec.input_schema),
            'output':app.registry.schema(spec.output_schema)})

    @op('discovery.diff',obj({'left':REF,'right':REF,'offset':INTEGER,'limit':{'type':'integer','minimum':1,'maximum':500}},
        ('left','right')),effect='read')
    async def diff(ctx,request,tx):
        refs=[decode(ResourceRef,request.arguments[key]) for key in ('left','right')]
        content=[]
        for ref in refs:
            await check_access(app,ctx,request,tx,ref.id,'read')
            revision=await tx.revision(ref)
            require(revision.content.media_type.startswith('text/') or revision.content.media_type=='application/json','text_diff_required')
            content.append((await app.contents.read_bytes(revision.content)).decode('utf-8').splitlines(keepends=True))
        lines=list(difflib.unified_diff(*content,fromfile=refs[0].id+'@'+str(refs[0].revision),tofile=refs[1].id+'@'+str(refs[1].revision)))
        offset,limit=request.arguments.get('offset',0),request.arguments.get('limit',100)
        return HandlerOutput(data={'diff':''.join(lines[offset:offset+limit]),'next_offset':offset+limit if offset+limit<len(lines) else None})

    @op('discovery.references',obj({'id':IDENTIFIER,'limit':{'type':'integer','minimum':1,'maximum':200}},('id',)),effect='read')
    async def references(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        items=[]
        for source,kind,body in tx.execute('SELECT rel.source_id,rel.type,rel.body FROM relations rel JOIN resources r ON r.id=rel.source_id AND r.revision=rel.revision_id WHERE rel.target_id=? ORDER BY rel.source_id',(rid,)):
            if await visible(app,ctx,request,tx,source):
                items.append({'source_id':source,'type':kind,'target':loads(body)['target']})
            if len(items)>=request.arguments.get('limit',50):
                break
        return HandlerOutput(data={'items':items})
    @op('discovery.raw',obj({'id':IDENTIFIER,'revision':IDENTIFIER,'offset':INTEGER,'length':INTEGER},('id',)),effect='read')
    async def raw(ctx,request,tx):
        a=request.arguments
        rid=await resolve(tx,a['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        resource=await tx.resource(rid)
        require(resource.state!='purged','resource_purged')
        ref=ResourceRef(id=rid,revision=a.get('revision'))
        revision=await tx.revision(ref)
        offset=a.get('offset',0)
        end=offset+a.get('length',revision.content.size-offset)
        require(0<=offset<=end<=revision.content.size,'invalid_byte_range')
        ref=ResourceRef(id=rid,revision=revision.id)
        return HandlerOutput(resources=(ref,),data={'content':wire(revision.content),'range':[offset,end],
            'filename':resource.name},output=ref)

    finish()
