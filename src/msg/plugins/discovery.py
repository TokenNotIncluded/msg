"""ACL-filtered reads and rebuildable discovery projections."""
from __future__ import annotations
import difflib
import fnmatch
import re
import time
from dataclasses import replace
from datetime import timedelta
from msg.constants import *
from msg.core.codec import canonical,wire,decode,loads,digest,b64
from msg.core.errors import Failure,require
from msg.core.models import Resource,ResourceRef,Revision,HandlerOutput,Credential
from msg.core.tags import normalize_tag
from msg.core.read_query import (ReadBudget,MAX_READ_DEPTH,NESTED_FIELDS,ROOT_FIELDS,
    expansion_schema,read_query_version)
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
        for field in ('change_note','source_kind','source_version','source_digest'):
            value=getattr(rev,field)
            if value is not None:
                data[field]=value
    return data


def short_subject_path(path):
    parts=path.split('/')
    if len(parts)>=3 and parts[1].startswith('@'):
        parts[2]={'keys':'k','certificates':'cert','keystore':'ks'}.get(parts[2],parts[2])
    return '/'.join(parts)


LINK_RELATIONS=frozenset({'self','t','a','r','p','c','f','q','b','h','v','d'})


async def visible_link(app,ctx,request,tx,ref):
    if not await visible(app,ctx,request,tx,ref.id):
        return None
    target=await tx.resource(ref.id)
    if target.state=='purged':
        return None
    if ref.revision is not None:
        try:
            await tx.revision(ref)
        except Failure as exc:
            if exc.code=='revision_not_found':
                return None
            raise
    return {'ref':wire(ref),'path':short_subject_path(await tx.path(ref.id))}


async def basic_links(app,ctx,request,tx,resource,revision):
    rid=resource.id
    current=ResourceRef(id=rid,revision=revision.id if revision is not None else resource.revision)
    singles={'self':{'ref':wire(current),'path':short_subject_path(await tx.path(rid))}}
    if resource.type in {'post','attachment','file'} and resource.parent:
        parent=await tx.resource(resource.parent)
        if parent.type=='topic':
            link=await visible_link(app,ctx,request,tx,ResourceRef(id=parent.id))
            if link:singles['t']=link
    if revision is not None:
        author=await visible_link(app,ctx,request,tx,ResourceRef(id=revision.author))
        if author:singles['a']=author
        singles['v']={'ref':wire(current),'path':f'/_r/{rid}/rev/{revision.id}'}
        if len(revision.parents)==1:
            singles['d']={'from':wire(ResourceRef(id=rid,revision=revision.parents[0])),
                          'to':wire(current),'path':f'/_r/{rid}/l/d'}
        outgoing={relation.type:relation.target for relation in revision.relations
                  if relation.type in {'reply_to','thread_root'}}
        if resource.type=='post':
            root=outgoing.get('thread_root',current)
            link=await visible_link(app,ctx,request,tx,root)
            if link:singles['r']=link
        if 'reply_to' in outgoing:
            link=await visible_link(app,ctx,request,tx,outgoing['reply_to'])
            if link:singles['p']=link
    return singles


async def relation_page(app,ctx,request,tx,resource,revision,rel,limit,position,snapshot, *, budget=None):
    if rel in {'c','b','f','q'} and position=='':
        position=['','']
    candidates=()
    if rel=='h':
        rows=tx.execute('''SELECT body FROM revisions WHERE resource_id=? AND id>?
            AND created_at<=? ORDER BY id LIMIT 2049''',(resource.id,position,wire(snapshot)))
        candidates=((item.id,ResourceRef(id=resource.id,revision=item.id))
                    for (raw,) in rows for item in (decode(Revision,loads(raw)),))
    elif rel in {'c','b'}:
        kinds=('reply_to',) if rel=='c' else ('quote','repost')
        placeholders=','.join('?' for _ in kinds)
        rows=tx.execute(f'''SELECT rel.source_id,rel.type,rel.revision_id
            FROM relations rel JOIN resources r ON r.id=rel.source_id AND r.revision=rel.revision_id
            WHERE rel.target_id=? AND rel.type IN ({placeholders}) AND r.state='active'
            AND r.created_at<=? AND (rel.source_id,rel.type)>(?,?)
            ORDER BY rel.source_id,rel.type LIMIT 2049''',
            (resource.id,*kinds,wire(snapshot),*position))
        candidates=(([source,kind],ResourceRef(id=source,revision=current_revision))
                    for source,kind,current_revision in rows)
    elif rel in {'f','q'} and revision is not None:
        kinds={'attachment'} if rel=='f' else {'quote','repost'}
        require(len(revision.relations)<=2048,'query_cost_exceeded')
        candidates=sorted(([relation.target.id,relation.type],relation.target)
                          for relation in revision.relations if relation.type in kinds)
    items=[]
    last=position
    more=False
    scanned=0
    for key,ref in candidates:
        if key<=position:
            continue
        scanned+=1
        require(scanned<=2048 and time.monotonic()<ctx.deadline_monotonic,'query_cost_exceeded')
        if budget is not None:budget.scan()
        link=await visible_link(app,ctx,request,tx,ref)
        if link is None:
            continue
        if rel=='h':
            historical=await tx.revision(ref)
            link.update(author=historical.author,created_at=wire(historical.created_at))
            for field in ('change_note','source_kind','source_version'):
                value=getattr(historical,field,None)
                if value is not None:link[field]=wire(value)
        if len(items)==limit:
            more=True
            break
        if budget is not None:budget.node(len(NESTED_FIELDS))
        items.append(link)
        last=key
    return items,last,more


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
    if resource.type=='post' and (not fields or 'links' in fields):
        active=await tx.revision(ResourceRef(id=rid,revision=revision))
        meta['links']=await basic_links(app,ctx,request,tx,resource,active)
    known=set(meta)
    if fields:
        require(set(fields)<=known,'unknown_projection_field')
        return {k:meta[k] for k in fields}
    defaults=('id','type','name','revision','generation','path','content','items','keys','certificates','links',
              'relations','raw_url','transfer_operation','kind','local_only','list_operation',
              'change_note','source_kind','source_version','source_digest')
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
                limit=a.get('limit',50)
                principal={'actor':ctx.principal.actor,'subject':ctx.principal.subject,
                           'credential_id':ctx.principal.credential_id}
                query={'id':rid,'view':'history','limit':limit}
                cursor,snapshot=app.cursors.decode_page(a['cursor'],'history',query,principal,ctx.now) if a.get('cursor') else (None,ctx.now)
                page=await tx.history(rid,cursor=cursor,limit=limit)
                revisions=[]
                for value in page.items:
                    row={'id':value.id,'parents':list(value.parents),'digest':value.manifest_digest,
                         'created_at':wire(value.created_at),'actor':value.actor,'author':value.author}
                    if value.change_note is not None:
                        row['change_note']=value.change_note
                    if value.source_version is not None:
                        row['source_version']=value.source_version
                    revisions.append(row)
                data={'id':rid,'revisions':revisions}
                if page.next_cursor:
                    cursor=app.cursors.encode_page('history',query,page.next_cursor,snapshot,
                        principal,ctx.now+timedelta(minutes=15))
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
    async def list_items(ctx,request,tx, *, arguments=None,budget=None):
        a=dict(request.arguments if arguments is None else arguments)
        stable=request.operation=='discovery.read_query'
        internal_page=getattr(request,'internal_page_state',None)
        if internal_page is not None:
            # Only the installed QueryRef adapter supplies this after verifying
            # its MAC, sealed source, principal, expiry and query digest.
            require(stable and set(internal_page)=={'arguments','last','snapshot'},
                    'invalid_cursor')
            a=dict(internal_page['arguments'])
        principal={'actor':ctx.principal.actor,'subject':ctx.principal.subject,
                   'credential_id':ctx.principal.credential_id}
        if stable and a.get('cursor'):
            saved_query,_=app.cursors.inspect_page(a['cursor'],ctx.now)
            require(saved_query.get('operation')==request.operation and
                    (read_query_version(saved_query.get('arguments',{}))==request.contract_version or
                     request.contract_version==2 and read_query_version(saved_query.get('arguments',{}))==1),
                    'cursor_query_mismatch')
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
        if stable and internal_page is not None:
            position,snapshot=internal_page['last'],parse_time(internal_page['snapshot'])
        elif stable and a.get('cursor'):
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
        scanned=0
        while len(values)<=limit:
            require(time.monotonic()<ctx.deadline_monotonic,'query_cost_exceeded')
            sql=f"SELECT r.body,{column},r.id FROM resources r WHERE {' AND '.join(filters)} AND ({column},r.id){comparison}(?,?) ORDER BY {column} {ordering},r.id {ordering} LIMIT 128"
            rows=tx.rows(sql,(*parameters,*last_position))
            if not rows:
                break
            for raw,order,rid in rows:
                scanned+=1
                require(scanned<=4096 and time.monotonic()<ctx.deadline_monotonic,'query_cost_exceeded')
                if budget is not None:budget.scan()
                last_position=[order,rid]
                resource=decode(Resource,loads(raw))
                if await visible(app,ctx,request,tx,rid):
                    if len(values)==limit:
                        more=True
                        break
                    chosen=a.get('fields',('id','type','name','revision','generation','path'))
                    if budget is not None:budget.node(len(chosen))
                    data=await metadata(tx,resource)
                    require(set(chosen)<=set(data),'unknown_projection_field')
                    values.append({k:data[k] for k in chosen})
                    position=last_position
            if more or len(rows)<128:
                break
        data={'items':values}
        if stable and request.contract_version==3 and values and not more:
            data['cursor']=app.cursors.encode_page(request.operation,query_args,position,snapshot,
                principal,ctx.now+timedelta(minutes=15))
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

    nested_schema=obj({**listing,
        'expand':{'type':'array','items':{'enum':['children','replies']},
                  'maxItems':2,'uniqueItems':True},
        'nested_first':{'type':'integer','minimum':1,'maximum':10},
        'collection':{'enum':['children','replies']}},())

    async def nested_page(ctx,request,tx,args, *, budget=None):
        """An independent, reauthorized page of one resource's collection."""
        principal={'actor':ctx.principal.actor,'subject':ctx.principal.subject,
                   'credential_id':ctx.principal.credential_id}
        a=dict(args)
        if a.get('cursor'):
            saved,_=app.cursors.inspect_page(a['cursor'],ctx.now)
            require(saved.get('operation')==request.operation and
                    saved.get('arguments',{}).get('collection') in {'children','replies'} and
                    set(a)=={'cursor'},'cursor_query_mismatch')
            a={**saved['arguments'],'cursor':a['cursor']}
        require(a.get('collection') in {'children','replies'} and a.get('parent') and
                not ({'expand','nested_first','type','author','query','tag','state',
                      'sort','direction'} & a.keys()),'invalid_nested_query')
        parent=await resolve(tx,a['parent'])
        collection=a['collection']
        limit=a.get('limit',5)
        require(1<=limit<=10 and set(a.get('fields',('id','name','type','path'))) <=
                {'id','name','type','path','revision'},'query_cost_exceeded')
        query_args={k:v for k,v in a.items() if k!='cursor'}
        if a.get('cursor'):
            position,snapshot=app.cursors.decode_page(a['cursor'],request.operation,
                                                       query_args,principal,ctx.now)
        else:
            position,snapshot=('',ctx.now) if collection=='children' else (['',''],ctx.now)
        # A parent whose read/list grant was revoked cannot be used to enumerate
        # descendants even if a previously issued cursor still has a valid MAC.
        await check_access(app,ctx,request,tx,parent,
                           'list' if collection=='children' else 'read')
        resource=await tx.resource(parent)
        require(resource.state=='active','ancestor_inactive')
        items=[]
        more=False
        last=position
        if collection=='children':
            scanned=0
            scan_position=position
            while len(items)<=limit:
                require(time.monotonic()<ctx.deadline_monotonic,'query_cost_exceeded')
                rows=tx.rows('''SELECT body,id FROM resources WHERE parent=? AND state='active'
                    AND created_at<=? AND id>? ORDER BY id LIMIT 128''',
                    (parent,wire(snapshot),scan_position))
                if not rows:
                    break
                for raw,rid in rows:
                    scanned+=1
                    require(scanned<=2048 and time.monotonic()<ctx.deadline_monotonic,'query_cost_exceeded')
                    if budget is not None:budget.scan()
                    scan_position=rid
                    if not await visible(app,ctx,request,tx,rid):
                        continue
                    if len(items)==limit:
                        more=True
                        break
                    child=decode(Resource,loads(raw))
                    value=await metadata(tx,child)
                    fields=a.get('fields',('id','name','type','path'))
                    if budget is not None:budget.node(len(fields))
                    items.append({key:value[key] for key in fields})
                    last=rid
                if more or len(rows)<128:
                    break
        else:
            revision=await tx.revision(ResourceRef(id=parent)) if resource.revision else None
            items,last,more=await relation_page(app,ctx,request,tx,resource,revision,
                                                'c',limit,position,snapshot,budget=budget)
        end_cursor=None
        if items:
            end_cursor=app.cursors.encode_page(request.operation,query_args,last,snapshot,
                principal,ctx.now+timedelta(minutes=15))
        page_info={'hasNextPage':more,'endCursor':end_cursor}
        data={'items':items,'pageInfo':page_info}
        if more:
            data['next']='/_r/c/'+end_cursor
            data['next_requires_auth']=ctx.principal.subject is not None
        return data

    @op('discovery.read_query',nested_schema,effect='read',version=2)
    async def read_query_v2(ctx,request,tx):
        a=dict(request.arguments)
        if a.get('cursor'):
            saved,_=app.cursors.inspect_page(a['cursor'],ctx.now)
            require(saved.get('operation')==request.operation and set(a)=={'cursor'} and
                    read_query_version(saved.get('arguments',{})) in {1,2},
                    'cursor_query_mismatch')
            if saved.get('arguments',{}).get('collection'):
                return HandlerOutput(data=await nested_page(ctx,request,tx,a))
            a=saved['arguments']
        if a.get('collection'):
            return HandlerOutput(data=await nested_page(ctx,request,tx,request.arguments))
        expand=a.get('expand',())
        require(not expand or 'id' in a.get('fields',('id',)),'unknown_projection_field')
        limit=a.get('limit',50)
        nested_first=a.get('nested_first',5)
        require(not expand or (limit<=10 and limit*len(expand)*(nested_first+1)<=100),
                'query_cost_exceeded')
        root_args=request.arguments if request.arguments.get('cursor') else {**a,'expand':list(expand)}
        result=await list_items(ctx,request,tx,arguments=root_args)
        data=dict(result.data)
        items=[]
        for source in data['items']:
            require(time.monotonic()<ctx.deadline_monotonic,'query_cost_exceeded')
            item=dict(source)
            if expand:
                item['collections']={}
                for collection in expand:
                    child_args={'parent':item['id'],'collection':collection,'limit':nested_first}
                    item['collections'][collection]=await nested_page(ctx,request,tx,child_args)
            items.append(item)
        data['items']=items
        data['pageInfo']={'hasNextPage':bool(data.get('next')),
                          'endCursor':data.get('cursor')}
        return HandlerOutput(data=data)

    tree_schema=obj({**listing,
        'query_version':{'const':3},
        'limit':{'type':'integer','minimum':1,'maximum':100},
        'fields':{'type':'array','items':{'enum':list(ROOT_FIELDS)},
                  'minItems':1,'maxItems':len(ROOT_FIELDS),'uniqueItems':True},
        'collection':{'enum':['children','replies']},
        'expand':expansion_schema()})

    @op('discovery.read_query',tree_schema,effect='read',version=3)
    async def read_query_v3(ctx,request,tx):
        budget=ReadBudget(ctx.deadline_monotonic,app.settings.server.limits.max_response_bytes)
        budget.check()
        principal={'actor':ctx.principal.actor,'subject':ctx.principal.subject,
                   'credential_id':ctx.principal.credential_id}
        supplied=dict(request.arguments)
        a=dict(supplied)
        position=snapshot=None
        if a.get('cursor'):
            require(set(a)=={'cursor'},'cursor_query_mismatch')
            saved,_=app.cursors.inspect_page(a['cursor'],ctx.now)
            require(saved.get('operation')==request.operation and
                    read_query_version(saved.get('arguments',{}))==3,'cursor_query_mismatch')
            a=dict(saved['arguments'])
            position,snapshot=app.cursors.decode_page(supplied['cursor'],request.operation,
                a,principal,ctx.now)
            # Cursor arguments are revalidated, not trusted as an open-ended
            # query language merely because their MAC is valid.
            app.registry.validate(tree_schema_ref,a)
        a['query_version']=3

        async def expand_page(page,plan,depth):
            require(depth<=MAX_READ_DEPTH,'query_cost_exceeded')
            values=[]
            for original in page['items']:
                budget.check()
                item=dict(original)
                if plan:
                    require('id' in item,'unknown_projection_field')
                    item['collections']={}
                    for collection,spec in plan.items():
                        child={'parent':item['id'],'collection':collection,
                               'limit':spec.get('limit',5),
                               'fields':spec.get('fields',list(NESTED_FIELDS)),
                               'expand':spec.get('expand',{}),'query_version':3}
                        item['collections'][collection]=await collection_page(child,depth+1)
                values.append(item)
            page['items']=values
            budget.output(page)
            return page

        async def collection_page(arguments,depth, *, last=None,boundary=None):
            require(depth<=MAX_READ_DEPTH,'query_cost_exceeded')
            args=dict(arguments)
            args['parent']=await resolve(tx,args['parent'])
            # The leaf reader is shared with v2; the public cursor additionally
            # binds this v3 expansion tree, its projection and stable parent ID.
            leaf={key:value for key,value in args.items() if key not in {'expand','query_version'}}
            if last is not None:
                inner=app.cursors.encode_page(request.operation,leaf,last,boundary,
                    principal,ctx.now+timedelta(minutes=15))
                leaf_request={'cursor':inner}
            else:
                leaf_request=leaf
            page=await nested_page(ctx,request,tx,leaf_request,budget=budget)
            if args['collection']=='replies':
                selected=args.get('fields',NESTED_FIELDS)
                values=[]
                for link in page['items']:
                    resource=await tx.resource(link['ref']['id'])
                    value=await metadata(tx,resource)
                    values.append({key:value[key] for key in selected})
                page['items']=values
            end=page['pageInfo']['endCursor']
            if end:
                leaf_query,_=app.cursors.inspect_page(end,ctx.now)
                last,boundary=app.cursors.decode_page(end,request.operation,
                    leaf_query['arguments'],principal,ctx.now)
                outer=app.cursors.encode_page(request.operation,args,last,boundary,
                    principal,ctx.now+timedelta(minutes=15))
                require(len(outer)<=8192,'query_cost_exceeded')
                page['pageInfo']['endCursor']=outer
                if page['pageInfo']['hasNextPage']:
                    page['next']='/_r/c/'+outer
            return await expand_page(page,args.get('expand',{}),depth)

        if a.get('collection'):
            require(a.get('parent'),'invalid_nested_query')
            data=await collection_page(a,0,last=position,boundary=snapshot)
        else:
            root_args=supplied if supplied.get('cursor') else a
            result=await list_items(ctx,request,tx,arguments=root_args,budget=budget)
            data=dict(result.data)
            data['pageInfo']={'hasNextPage':bool(data.get('next')),
                              'endCursor':data.get('cursor')}
            data=await expand_page(data,a.get('expand',{}),0)
        return HandlerOutput(data=data)

    tree_schema_ref=ResourceRef(id='schema:discovery.read_query:3')

    lexical_fields={'type':'array','items':STRING,'maxItems':12,'uniqueItems':True}
    lexical_facets={'type':'array','items':{'enum':['type','tag']},
                    'maxItems':2,'uniqueItems':True}
    lexical_schema=obj({'scope':IDENTIFIER,'terms':STRING,'exact':STRING,'not_terms':STRING,
        'mode':{'enum':['all','any']},'field':{'enum':['all','name','body','metadata']},
        'type':STRING,'owner':IDENTIFIER,'author':IDENTIFIER,'tag':STRING,
        'state':{'enum':['active','archived']},'created_after':STRING,'created_before':STRING,
        'updated_after':STRING,'updated_before':STRING,'has_attachment':BOOLEAN,
        'depth':{'type':'integer','minimum':0,'maximum':5},'recursive':BOOLEAN,
        'order':{'enum':['relevance','updated','created','name']},
        'limit':{'type':'integer','minimum':1,'maximum':100},'cursor':STRING,
        'snippet':BOOLEAN,'explain':{'enum':['compact']},'fields':lexical_fields,
        'facets':lexical_facets})
    lexical_schema_v1={**lexical_schema,
                       'properties':{name:value for name,value in lexical_schema['properties'].items()
                                     if name!='facets'}}
    lexical_schema_v3={**lexical_schema,
                       'properties':{**lexical_schema['properties'],
                           'source_kind':{'enum':['release','user','operation']},
                           'relation_type':{'enum':['reply_to','thread_root','quote','repost',
                                                    'attachment','template']}}}
    lexical_schema_v4={**lexical_schema_v3,
                       'properties':{**lexical_schema_v3['properties'],
                                     'suggest':BOOLEAN}}

    @op('discovery.lexical_search',lexical_schema_v1,effect='read')
    @op('discovery.lexical_search',lexical_schema,effect='read',version=2)
    @op('discovery.lexical_search',lexical_schema_v3,effect='read',version=3)
    @op('discovery.lexical_search',lexical_schema_v4,effect='read',version=4)
    async def lexical_search(ctx,request,tx):
        a=dict(request.arguments)
        internal_page=getattr(request,'internal_page_state',None)
        if internal_page is not None:
            require(set(internal_page)=={'arguments','last','snapshot'},'invalid_cursor')
            a=dict(internal_page['arguments'])
        principal={'actor':ctx.principal.actor,'subject':ctx.principal.subject,
                   'credential_id':ctx.principal.credential_id}
        if a.get('cursor'):
            saved,_=app.cursors.inspect_page(a['cursor'],ctx.now)
            require(saved.get('operation')==request.operation and set(a)=={'cursor'},
                    'cursor_query_mismatch')
            a={**saved['arguments'],'cursor':a['cursor']}
        # A cursor or sealed QueryRef carries arguments from an earlier call.
        # Keep those arguments inside the version selected for this call too.
        require(not (request.contract_version<4 and 'suggest' in a) and
                not (request.contract_version<3 and
                     {'source_kind','relation_type'}&a.keys()) and
                not (request.contract_version<2 and 'facets' in a),
                'cursor_query_mismatch')
        require(a.get('scope') is not None,'search_scope_required')
        scope=await resolve(tx,a['scope'])
        await check_access(app,ctx,request,tx,scope,'list')
        a['scope']=scope
        if a.get('tag'):
            a['tag']=normalize_tag(a['tag'])
        for field in ('terms','exact','not_terms'):
            require(len(a.get(field,''))<=512,'query_cost_exceeded')
        terms=a.get('terms','').casefold().split()
        excluded=a.get('not_terms','').casefold().split()
        exact=a.get('exact','').casefold()
        require((terms or exact) and len(terms)<=8 and len(excluded)<=8,
                'search_query_required')
        limit=a.get('limit',50)
        selected=a.get('fields',('id','path','type','name','revision','author','created_at'))
        require(set(selected)<=set(('id','path','type','name','revision','author',
                                    'owner','created_at','modified_at','score','rank_reason','snippet','links')),
                'unknown_projection_field')
        require(limit*(len(selected)+2)<=1200,'query_cost_exceeded')
        normalized={key:value for key,value in a.items() if key!='cursor'}
        if internal_page is not None:
            position,snapshot=internal_page['last'],parse_time(internal_page['snapshot'])
        elif a.get('cursor'):
            position,snapshot=app.cursors.decode_page(a['cursor'],request.operation,
                                                       normalized,principal,ctx.now)
        else:
            position,snapshot=[],ctx.now
        cutoff={key:parse_time(a[key]) for key in ('created_after','created_before',
            'updated_after','updated_before') if key in a}
        scope_resource=await tx.resource(scope)
        owner=await resolve(tx,a['owner']) if a.get('owner') else None
        author=await resolve(tx,a['author']) if a.get('author') else None
        results=[]
        facet_counts={name:{} for name in a.get('facets',())}
        # Suggestion counts describe *matched readable resources*, not raw
        # indexed terms. Rebuild on every page so revoked grants disappear.
        suggestions={}
        suggest_prefix=terms[-1] if a.get('suggest') and terms else ''
        if a.get('suggest'):
            require(2<=len(suggest_prefix)<=32,'query_cost_exceeded')
        scanned=0
        # Restrict the SQL candidate set before applying the work budget. A
        # global LIMIT lets unrelated (or unreadable) rows starve a small scope.
        owner_scope=scope_resource.type in {'user','organization'}
        candidates=tx.execute('''WITH RECURSIVE subtree(id,depth) AS (
                SELECT id,0 FROM resources WHERE id=?
                UNION ALL
                SELECT r.id,s.depth+1 FROM resources r JOIN subtree s ON r.parent=s.id
                WHERE s.depth<5
            ) SELECT body FROM resources WHERE created_at<=? AND
                (id IN (SELECT id FROM subtree) OR (? AND (owner=? OR grp=?)))
            ORDER BY id''',(scope,wire(snapshot),owner_scope,scope,scope))
        for (raw,) in candidates:
            require(time.monotonic()<ctx.deadline_monotonic,'query_cost_exceeded')
            resource=decode(Resource,loads(raw))
            if resource.state!=a.get('state','active'):
                continue
            chain=await tx.ancestors(resource.id)
            ancestors=[item.id for item in chain]
            scoped_owner=(scope_resource.type in {'user','organization'} and
                          (resource.owner==scope or resource.group==scope))
            if resource.id!=scope and scope not in ancestors and not scoped_owner:
                continue
            distance=len(ancestors)-ancestors.index(scope) if scope in ancestors else 0
            if distance>a.get('depth',5) or (not a.get('recursive',True) and distance>1):
                continue
            if a.get('type') and resource.type!=a['type']:
                continue
            if owner and resource.owner!=owner:
                continue
            if a.get('tag') and a['tag'] not in resource.tags:
                continue
            if ('created_after' in cutoff and resource.created_at<cutoff['created_after'] or
                    'created_before' in cutoff and resource.created_at>cutoff['created_before'] or
                    'updated_after' in cutoff and resource.modified_at<cutoff['updated_after'] or
                    'updated_before' in cutoff and resource.modified_at>cutoff['updated_before']):
                continue
            if not await visible(app,ctx,request,tx,resource.id):
                continue
            scanned+=1
            require(scanned<=2000,'query_cost_exceeded')
            revision=await tx.revision(ResourceRef(id=resource.id)) if resource.revision else None
            # These predicates inspect only the current revision of an already
            # readable resource. Historical relations and source metadata must
            # not affect rank, facets or page positions.
            if a.get('source_kind') and (revision is None or
                                         revision.source_kind!=a['source_kind']):
                continue
            if a.get('relation_type') and (revision is None or not any(
                    relation.type==a['relation_type'] for relation in revision.relations)):
                continue
            if author and (revision is None or revision.author!=author):
                continue
            if a.get('has_attachment') is not None and bool(revision and any(
                    relation.type=='attachment' for relation in revision.relations))!=a['has_attachment']:
                continue
            name=resource.name
            body=''
            if a.get('field','all') in {'all','body'} and revision and revision.content.media_type.startswith('text/'):
                require(revision.content.size<=65536,'query_cost_exceeded')
                body=(await app.contents.read_bytes(revision.content)).decode('utf-8')
            meta=f'{resource.type} {resource.owner} {resource.group}'
            selected_text={'name':name,'body':body,'metadata':meta}
            field=a.get('field','all')
            active=selected_text if field=='all' else {field:selected_text[field]}
            lowered={key:value.casefold() for key,value in active.items()}
            whole=' '.join(lowered.values())
            if terms and not (all(term in whole for term in terms) if a.get('mode','all')=='all'
                              else any(term in whole for term in terms)):
                continue
            if exact and exact not in whole:
                continue
            if any(term in whole for term in excluded):
                continue
            score=sum((5 if key=='name' else 1)*sum(value.count(term) for term in terms)
                      for key,value in lowered.items())+(3 if exact else 0)
            order=a.get('order','relevance')
            if order=='relevance':
                sort_key=[-score,-int(resource.modified_at.timestamp()*1000000),resource.id]
            elif order=='updated':
                sort_key=[-int(resource.modified_at.timestamp()*1000000),resource.id]
            elif order=='created':
                sort_key=[-int(resource.created_at.timestamp()*1000000),resource.id]
            else:
                sort_key=[resource.name.casefold(),resource.id]
            path=short_subject_path(await tx.path(resource.id))
            item={'ref':wire(ResourceRef(id=resource.id,revision=resource.revision)),
                  'id':resource.id,'path':path,'type':resource.type,'name':name,
                  'revision':resource.revision,'author':revision.author if revision else None,
                  'owner':resource.owner,'created_at':wire(resource.created_at),
                  'modified_at':wire(resource.modified_at),'score':score}
            if a.get('snippet'):
                for key,value in active.items():
                    low=lowered[key]
                    needle=exact if exact and exact in low else next((term for term in terms if term in low),'')
                    if needle:
                        start=low.index(needle)
                        left=max(0,start-40)
                        right=min(len(value),start+len(needle)+40)
                        item['snippet']={'field':key,'text':value[left:right],
                                         'range':[start-left,start-left+len(needle)]}
                        break
            if a.get('explain')=='compact':
                item['rank_reason']={'matched_fields':[key for key,value in lowered.items()
                                                        if any(term in value for term in terms) or
                                                           bool(exact and exact in value)],
                                     'terms':terms,'order':order}
            if 'links' in selected:
                from msg.plugins.discovery import basic_links
                item['links']=await basic_links(app,ctx,request,tx,resource,revision)
            if a.get('fields'):
                item={key:item[key] for key in selected if key in item}
            results.append((sort_key,item))
            if a.get('suggest'):
                # Names alone keep this optional projection small and avoid
                # mining arbitrary body text. A resource contributes at most
                # once to each candidate, regardless of repeated words.
                require(len(name)<=256,'query_cost_exceeded')
                words={word.casefold() for word in re.findall(r'[\w-]+',name)}
                for word in words:
                    if (suggest_prefix!=word and word.startswith(suggest_prefix)
                            and len(word)<=32):
                        suggestions[word]=suggestions.get(word,0)+1
                        require(len(suggestions)<=100,'query_cost_exceeded')
            # Aggregate only matched resources after the current read grant was
            # checked. Never derive buckets from the SQL candidates or a
            # previous page cursor: grants can disappear between page reads.
            for name,values in facet_counts.items():
                keys=(resource.type,) if name=='type' else resource.tags
                for key in keys:
                    values[key]=values.get(key,0)+1
                    require(len(values)<=20,'query_cost_exceeded')
        results.sort(key=lambda row:row[0])
        following=[row for row in results if not position or row[0]>position]
        page=following[:limit]
        data={'items':[item for _,item in page]}
        if facet_counts:
            data['facets']={name:[{'value':key,'count':count}
                                  for key,count in sorted(values.items(),
                                      key=lambda pair:(-pair[1],pair[0]))]
                            for name,values in facet_counts.items()}
        if a.get('suggest'):
            data['suggestions']=[{'value':word,'count':count}
                                 for word,count in sorted(suggestions.items(),
                                     key=lambda pair:(-pair[1],pair[0]))[:10]]
        if len(following)>limit:
            cursor=app.cursors.encode_page(request.operation,normalized,page[-1][0],snapshot,
                principal,ctx.now+timedelta(minutes=15))
            data.update(cursor=cursor,next='/_r/c/'+cursor,
                        next_requires_auth=ctx.principal.subject is not None)
        return HandlerOutput(data=data)

    @op('discovery.grep',obj({'scope':IDENTIFIER,'pattern':STRING,'regex':BOOLEAN,
        'glob':STRING,'exclude_glob':STRING,'case_sensitive':BOOLEAN,
        'before':{'type':'integer','minimum':0,'maximum':3},
        'after':{'type':'integer','minimum':0,'maximum':3},
        'max_matches':{'type':'integer','minimum':1,'maximum':100},
        'max_files':{'type':'integer','minimum':1,'maximum':100},
        'files_with_matches':BOOLEAN,'count_only':BOOLEAN},('scope','pattern')),effect='read')
    async def grep(ctx,request,tx):
        a=request.arguments
        scope=await resolve(tx,a['scope'])
        await check_access(app,ctx,request,tx,scope,'list')
        pattern=a['pattern']
        require(0<len(pattern)<=128,'invalid_grep_pattern')
        regular=a.get('regex',False)
        if regular:
            # Keep the public regex subset free of backtracking quantifiers,
            # groups, alternation and backreferences.
            require(not any(char in pattern for char in '(){}|\\*+?'),
                    'invalid_grep_pattern')
            try:
                expression=re.compile(pattern,0 if a.get('case_sensitive',True) else re.IGNORECASE)
            except re.error as exc:
                raise Failure('invalid_grep_pattern') from exc
        else:
            expression=(re.compile(re.escape(pattern),re.IGNORECASE)
                        if not a.get('case_sensitive',True) else None)
        max_files=a.get('max_files',50)
        max_matches=a.get('max_matches',50)
        seen_files=0
        bytes_read=0
        matches=[]
        files=[]
        count=0
        truncated=False
        scanned=0
        candidates=tx.execute('''WITH RECURSIVE subtree(id) AS (
                SELECT id FROM resources WHERE id=?
                UNION ALL SELECT r.id FROM resources r JOIN subtree s ON r.parent=s.id
            ) SELECT body FROM resources WHERE state='active'
                AND id IN (SELECT id FROM subtree) ORDER BY id''',(scope,))
        for (raw,) in candidates:
            require(time.monotonic()<ctx.deadline_monotonic,'query_cost_exceeded')
            resource=decode(Resource,loads(raw))
            if resource.id!=scope and scope not in {item.id for item in await tx.ancestors(resource.id)}:
                continue
            if not await visible(app,ctx,request,tx,resource.id):
                continue
            if not resource.revision:
                continue
            path=short_subject_path(await tx.path(resource.id))
            if a.get('glob') and not fnmatch.fnmatchcase(path,a['glob']):
                continue
            if a.get('exclude_glob') and fnmatch.fnmatchcase(path,a['exclude_glob']):
                continue
            scanned+=1
            require(scanned<=2000,'query_cost_exceeded')
            revision=await tx.revision(ResourceRef(id=resource.id))
            if not revision.content.media_type.startswith('text/'):
                continue
            require(revision.content.size<=65536,'query_cost_exceeded')
            if seen_files>=max_files:
                truncated=True
                break
            seen_files+=1
            bytes_read+=revision.content.size
            require(bytes_read<=1048576,'query_cost_exceeded')
            lines=(await app.contents.read_bytes(revision.content)).decode('utf-8').splitlines()
            found_file=False
            for line_no,line in enumerate(lines,1):
                if expression is not None:
                    positions=((found.start(),found.end()) for found in expression.finditer(line))
                else:
                    def positions_in_line():
                        offset=0
                        while (start:=line.find(pattern,offset))>=0:
                            yield start,start+len(pattern)
                            offset=start+len(pattern)
                    positions=positions_in_line()
                for start,end in positions:
                    count+=1
                    if not found_file:
                        files.append({'ref':wire(ResourceRef(id=resource.id,revision=revision.id)),
                                      'path':path})
                        found_file=True
                    if not (a.get('count_only') or a.get('files_with_matches')):
                        left=max(0,line_no-1-a.get('before',0))
                        right=min(len(lines),line_no+a.get('after',0))
                        matches.append({'ref':wire(ResourceRef(id=resource.id,revision=revision.id)),
                            'path':path,'line_hint':line_no,'range':[start,end],
                            'context':'\n'.join(lines[left:right])[:512]})
                    if count>=max_matches:
                        truncated=True
                        break
                if truncated:
                    break
            if truncated:
                break
        if a.get('count_only'):
            return HandlerOutput(data={'count':count,'truncated':truncated})
        if a.get('files_with_matches'):
            return HandlerOutput(data={'files':files,'truncated':truncated})
        return HandlerOutput(data={'matches':matches,'truncated':truncated})

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
        description=app.registry.describe(spec)
        return HandlerOutput(data={'operation':description,'input':app.registry.schema(spec.input_schema),
            'output':app.registry.schema(spec.output_schema),
            'requires_rules':description['requires_rules']})

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

    @op('discovery.links',obj({'id':IDENTIFIER,'rel':{'enum':sorted(LINK_RELATIONS)},
        'cursor':STRING,'limit':{'type':'integer','minimum':1,'maximum':100}}),effect='read')
    async def links(ctx,request,tx):
        args=dict(request.arguments)
        principal={'actor':ctx.principal.actor,'subject':ctx.principal.subject,
                   'credential_id':ctx.principal.credential_id}
        if args.get('cursor'):
            saved,_=app.cursors.inspect_page(args['cursor'],ctx.now)
            require(saved.get('operation')=='discovery.links' and set(args)=={'cursor'},
                    'cursor_query_mismatch')
            args={**saved['arguments'],'cursor':args['cursor']}
        require(args.get('id') is not None,'read_resource_required')
        rid=await resolve(tx,args['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        resource=await tx.resource(rid)
        require(resource.state!='purged','resource_purged')
        revision=await tx.revision(ResourceRef(id=rid)) if resource.revision else None
        rel=args.get('rel')
        if rel is not None:
            require(rel in LINK_RELATIONS,'unknown_link_relation')
        limit=args.get('limit',50)
        query_args={'id':rid,'rel':rel,'limit':limit}
        if args.get('cursor'):
            position,snapshot=app.cursors.decode_page(args['cursor'],request.operation,
                                                       query_args,principal,ctx.now)
        else:
            position,snapshot='',ctx.now
        current=ResourceRef(id=rid,revision=resource.revision)
        singles=await basic_links(app,ctx,request,tx,resource,revision)
        collections={'c','f','q','b','h'}
        if rel in collections:
            items,last,more=await relation_page(app,ctx,request,tx,resource,revision,rel,
                                                limit,position,snapshot)
            data={'id':rid,'revision':resource.revision,'rel':rel,'items':items}
            if more:
                cursor=app.cursors.encode_page(request.operation,query_args,last,snapshot,principal,
                                               ctx.now+timedelta(minutes=15))
                data.update(cursor=cursor,next='/_r/c/'+cursor,
                            next_requires_auth=ctx.principal.subject is not None)
            return HandlerOutput(data=data)
        if rel is not None:
            require(rel in singles,'not_found')
            return HandlerOutput(data=singles[rel])
        linkset=dict(singles)
        for kind in sorted(collections):
            items,_,_=await relation_page(app,ctx,request,tx,resource,revision,kind,1,'',ctx.now)
            if items:
                linkset[kind]={'source':wire(current),'path':f'/_r/{rid}/l/{kind}',
                               'collection':True}
        return HandlerOutput(data={'id':rid,'revision':resource.revision,'links':linkset})

    @op('discovery.diff_view',obj({'id':IDENTIFIER,'known_revision':IDENTIFIER,
        'old_revision':IDENTIFIER,'new_revision':IDENTIFIER,'previous':BOOLEAN,
        'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':500}},
        ('id',)),effect='read')
    async def diff_view(ctx,request,tx):
        args=request.arguments
        rid=await resolve(tx,args['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        resource=await tx.resource(rid)
        require(resource.state!='purged' and resource.revision is not None,'revision_not_found')
        variants=sum((bool(args.get('previous')),bool(args.get('known_revision')),
                      bool(args.get('old_revision') or args.get('new_revision'))))
        require(variants==1,'invalid_diff_range')
        if args.get('previous'):
            current=await tx.revision(ResourceRef(id=rid,revision=resource.revision))
            require(len(current.parents)==1,'previous_revision_not_found')
            old,new=current.parents[0],current.id
        elif args.get('known_revision'):
            old,new=args['known_revision'],resource.revision
        else:
            require(bool(args.get('old_revision')) and bool(args.get('new_revision')),
                    'invalid_diff_range')
            old,new=args['old_revision'],args['new_revision']
        before=await tx.revision(ResourceRef(id=rid,revision=old))
        after=await tx.revision(ResourceRef(id=rid,revision=new))
        for revision in (before,after):
            require(revision.content.media_type.startswith('text/') or
                    revision.content.media_type=='application/json','text_diff_required')
            require(revision.content.size<=4*app.settings.server.limits.max_response_bytes,
                    'diff_requires_transfer')
        prior_bytes=await app.contents.read_bytes(before.content)
        current_bytes=await app.contents.read_bytes(after.content)
        require(prior_bytes.count(b'\n')+current_bytes.count(b'\n')<=20000,
                'query_cost_exceeded')
        prior=prior_bytes.decode('utf-8').splitlines(keepends=True)
        current=current_bytes.decode('utf-8').splitlines(keepends=True)
        path=short_subject_path(await tx.path(rid))
        lines=list(difflib.unified_diff(prior,current,fromfile=path+'@'+old,tofile=path+'@'+new))
        offset,limit=args.get('offset',0),args.get('limit',500)
        data={'from':wire(ResourceRef(id=rid,revision=old)),
              'to':wire(ResourceRef(id=rid,revision=new)),
              'diff':''.join(lines[offset:offset+limit])}
        for label,revision in (('from_source',before),('to_source',after)):
            source={name:getattr(revision,name) for name in
                ('change_note','source_kind','source_version','source_digest')
                if getattr(revision,name) is not None}
            if source:
                data[label]=source
        if offset+limit<len(lines):
            data['next_offset']=offset+limit
            data['next']=f'/_r/{rid}/diff/{old}/{new}/o/{offset+limit}'
        return HandlerOutput(data=data)

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
