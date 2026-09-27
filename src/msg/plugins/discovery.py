"""ACL-filtered reads and rebuildable discovery projections."""
from __future__ import annotations
import difflib
from dataclasses import replace
from datetime import timedelta
from msg.constants import *
from msg.core.codec import canonical,wire,decode,loads,digest,b64
from msg.core.errors import Failure,require
from msg.core.models import Resource,ResourceRef,HandlerOutput,Credential
from msg.core.tags import normalize_tag
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
    data['path']=short_subject_path(await tx.path(r.id))
    if r.revision:
        rev=await tx.revision(ResourceRef(id=r.id))
        data.update(size=rev.content.size,media_type=rev.content.media_type,digest=rev.content.digest)
    return data


def short_subject_path(path):
    parts=path.split('/')
    if len(parts)>=3 and parts[1].startswith('@'):
        parts[2]={'keys':'k','certificates':'cert','keystore':'ks'}.get(parts[2],parts[2])
    return '/'.join(parts)


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
        return {'id':rid,'path':meta['path'],'keys':keys}
    if resource.name=='certificates' and resource.parent is not None and (await tx.resource(resource.parent)).type=='user':
        return {'id':rid,'path':meta['path'],'certificates':[{'id':row[0],'revoked':bool(row[1])} for row in
            tx.rows('SELECT id,revoked FROM certificates WHERE subject=? ORDER BY id',(resource.parent,))]}
    if app.registry.resource_type(resource.type,1).container and resource.type not in {'repo','website'}:
        values=[]
        cursor=None
        while len(values)<50:
            page=await tx.children(rid,cursor,50)
            for child in page.items:
                if child.state=='active' and await visible(app,ctx,request,tx,child.id):
                    values.append({'id':child.id,'name':child.name,'type':child.type,'revision':child.revision,
                                   'path':short_subject_path(await tx.path(child.id))})
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
    if resource.tags:
        defaults=(*defaults,'tags')
    return {k:meta[k] for k in defaults if k in meta}


async def markdown_segment(app,blob,offset,max_bytes,fence=False,line_start=True):
    """Read a bounded UTF-8 window, preferring complete Markdown blocks."""
    require(0<=offset<=blob.size,'invalid_byte_range')
    end=min(blob.size,offset+max_bytes+4)
    raw=b''.join([piece async for piece in app.contents.read(blob,(offset,end))])
    decoded=None
    for trim in range(4):
        try:
            decoded=raw[:len(raw)-trim if trim else len(raw)].decode('utf-8')
            break
        except UnicodeDecodeError as exc:
            require(exc.start>=len(raw)-4,'invalid_utf8_content')
    require(decoded is not None,'invalid_utf8_content')
    used=0
    count=0
    for char in decoded:
        length=len(char.encode('utf-8'))
        if used+length>max_bytes:
            break
        used+=length
        count+=1
    budget=decoded[:count]
    require(bool(budget) or offset==blob.size,'read_window_too_small')
    candidate=None
    scanned=0
    in_fence=fence
    at_line_start=line_start
    state=(in_fence,at_line_start)
    for line in budget.splitlines(keepends=True):
        complete=line.endswith('\n')
        stripped=line.strip()
        if at_line_start and stripped.startswith(('```','~~~')):
            in_fence=not in_fence
            if not in_fence and complete:
                candidate=(scanned+len(line),in_fence,True)
        elif not in_fence and at_line_start and line.startswith('#') and scanned:
            candidate=(scanned,in_fence,True)
        elif not in_fence and not stripped and complete:
            candidate=(scanned+len(line),in_fence,True)
        scanned+=len(line)
        at_line_start=complete
        state=(in_fence,at_line_start)
    if candidate is not None and candidate[0]>0:
        char_end,fence_end,line_end=candidate
        continued=False
    else:
        char_end=count
        fence_end,line_end=state
        continued=offset+len(budget.encode('utf-8'))<blob.size
    text=budget[:char_end]
    byte_end=offset+len(text.encode('utf-8'))
    require(byte_end>offset or offset==blob.size,'read_window_too_small')
    return byte_end,text,fence_end,line_end,continued


async def previous_segment(app,blob,target,max_bytes):
    offset=0
    fence=False
    line_start=True
    continued=False
    previous=None
    while offset<target:
        previous=(offset,fence,line_start,continued)
        end,_,fence,line_start,continued=await markdown_segment(
            app,blob,offset,max_bytes,fence,line_start)
        require(end<=target,'invalid_cursor')
        offset=end
    require(offset==target and previous is not None,'invalid_cursor')
    return previous


def next_link(app,operation,args):
    packet=request_for(operation,args,app.settings.service_url,source='manual',
                       request_id='read_'+digest((operation,args))[7:39])
    packet=replace(packet,expires_at=None)
    return f'/-/g/{operation}/j/'+b64(canonical(packet))


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

    @op('discovery.read_segment',obj({'id':IDENTIFIER,'revision':IDENTIFIER,
        'max_bytes':{'type':'integer','minimum':32,'maximum':8192},'cursor':STRING}),effect='read')
    async def read_segment(ctx,request,tx):
        a=request.arguments
        principal={'actor':ctx.principal.actor,'subject':ctx.principal.subject,
                   'credential_id':ctx.principal.credential_id}
        if a.get('cursor'):
            require(set(a)=={'cursor'},'cursor_query_mismatch')
            query,position=app.cursors.decode_read(a['cursor'],principal,ctx.now)
            ref=decode(ResourceRef,query['ref'])
            max_bytes=query['max_bytes']
            offset=position['offset']
            fence=position['fence']
            line_start=position['line_start']
            continues_previous=position['continued']
            previous=position.get('previous')
        else:
            require(a.get('id') is not None,'read_resource_required')
            rid=await resolve(tx,a['id'])
            ref=ResourceRef(id=rid,revision=a.get('revision'))
            max_bytes=a.get('max_bytes',4096)
            offset=0
            fence=False
            line_start=True
            continues_previous=False
            previous=None
        require(32<=max_bytes<=min(8192,app.settings.server.limits.max_response_bytes//4),
                'query_cost_exceeded')
        await check_access(app,ctx,request,tx,ref.id,'read')
        revision=await tx.revision(ref)
        require(revision.content.media_type.startswith('text/'),'text_required')
        ref=ResourceRef(id=ref.id,revision=revision.id)
        end,text,fence_end,line_end,continued=await markdown_segment(
            app,revision.content,offset,max_bytes,fence,line_start)
        data={'id':ref.id,'revision':revision.id,'range':[offset,end],
              'text':text,'continued_block':continued,'continues_previous':continues_previous}
        if end<revision.content.size:
            cursor=app.cursors.encode_read(ref,max_bytes,end,fence_end,line_end,continued,
                                          principal,ctx.now+timedelta(minutes=15),
                                          previous=[offset,fence,line_start,continues_previous])
            data['next']='/_r/c/'+cursor
        if offset>0:
            if previous is None:
                previous=await previous_segment(app,revision.content,offset,max_bytes)
            previous_offset,previous_fence,previous_line,previous_continued=previous
            cursor=app.cursors.encode_read(ref,max_bytes,previous_offset,previous_fence,
                                          previous_line,previous_continued,principal,
                                          ctx.now+timedelta(minutes=15))
            data['prev']='/_r/c/'+cursor
        return HandlerOutput(resources=(ref,),data=data)

    listing={'parent':IDENTIFIER,'type':STRING,'author':IDENTIFIER,'query':STRING,'tag':STRING,
             'state':{'enum':['active','archived','purged']},
             'sort':{'enum':['id','time','name']},'direction':{'enum':['asc','desc']},'limit':{'type':'integer','minimum':1,'maximum':200},'cursor':STRING,'fields':fields}
    async def list_items(ctx,request,tx):
        a=dict(request.arguments)
        stable=request.operation=='discovery.read_query'
        principal={'actor':ctx.principal.actor,'subject':ctx.principal.subject,
                   'credential_id':ctx.principal.credential_id}
        if stable and a.get('cursor'):
            saved_query,_=app.cursors.inspect_page(a['cursor'],ctx.now)
            require(saved_query.get('operation')==request.operation,'cursor_query_mismatch')
            supplied={k:v for k,v in a.items() if k!='cursor'}
            require(not supplied,'cursor_query_mismatch')
            a={**saved_query['arguments'],'cursor':a['cursor']}
        if a.get('tag') is not None:
            a['tag']=normalize_tag(a['tag'])
        parent=await resolve(tx,a['parent']) if a.get('parent') else None
        if stable and parent:
            a['parent']=parent
        if parent==TOOLS_SPACE:
            return HandlerOutput(data={'items':await filtered_tools(app,ctx,request,tx)})
        if parent:
            await check_access(app,ctx,request,tx,parent,'list')
        limit=a.get('limit',50)
        if stable:
            fields_count=len(a.get('fields',('id','type','name','revision','generation','path')))
            require(limit<=100 and limit*(fields_count+1)<=1000,'query_cost_exceeded')
        query_hash=digest({k:v for k,v in a.items() if k!='cursor'})
        sort=a.get('sort','id')
        column={'id':'r.id','time':'r.created_at','name':'r.name'}[sort]
        descending=a.get('direction','asc')=='desc'
        comparison,ordering=('<','DESC') if descending else ('>','ASC')
        query_args={k:v for k,v in a.items() if k!='cursor'}
        if stable and a.get('cursor'):
            position,snapshot=app.cursors.decode_page(a['cursor'],request.operation,
                                                       query_args,principal,ctx.now)
        else:
            position=app.cursors.decode(a['cursor'],'page',query_hash) if a.get('cursor') else (['\uffff','\uffff'] if descending else ['', ''])
            snapshot=ctx.now
        filters=['r.state=?']
        parameters=[a.get('state','active')]
        if stable:
            filters.append('r.created_at<=?');parameters.append(wire(snapshot))
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
        if a.get('tag'):
            filters.append('EXISTS (SELECT 1 FROM resource_tags rt WHERE rt.resource_id=r.id AND rt.tag=?)')
            parameters.append(a['tag'])
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
            if stable:
                cursor=app.cursors.encode_page(request.operation,query_args,position,snapshot,
                    principal,ctx.now+timedelta(minutes=15))
                data.update(cursor=cursor,next='/_r/c/'+cursor,
                            next_requires_auth=ctx.principal.subject is not None)
            else:
                cursor=app.cursors.encode('page',query_hash,position)
                data.update(cursor=cursor,next=next_link(app,request.operation,{**a,'cursor':cursor}),
                            next_requires_auth=ctx.principal.subject is not None)
        return HandlerOutput(data=data)
    op('discovery.list',obj(listing),effect='read')(list_items)
    op('discovery.search',obj(listing,('query',)),effect='read')(list_items)
    op('discovery.read_query',obj(listing),effect='read')(list_items)

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
        name=request.arguments['operation']
        operation,separator,version=name.rpartition('@')
        if separator:
            require(version.isdecimal() and int(version)>0,'invalid_operation_version')
            spec=app.registry.operation(operation,int(version))
        else:
            spec=app.registry.operation(name)
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
