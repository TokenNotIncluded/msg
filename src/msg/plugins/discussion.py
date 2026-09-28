"""Replies are posts; likes and ACK are explicit authenticated facts."""
from __future__ import annotations
from msg.core.codec import canonical,decode,wire,digest,loads,b64
from msg.core.errors import Failure,require
from msg.core.models import ResourceRef,Relation,HandlerOutput
from msg.core.requests import signing_bytes
from msg.plugins.common import *
from msg.plugins.schemas import *
from msg.plugins.content import create_post
from msg.plugins.discovery import visible,read_projection
from msg.security.policy import CERTGATE


async def gates(app,ctx,request,tx,rid):
    await app.authorizer._ceiling(ctx.principal,operation_id(request),rid,tx)
    chain=(*await tx.ancestors(rid),await tx.resource(rid))
    if any(r.mode&CERTGATE for r in chain):
        require(await app.authorizer.has(ctx.principal,'resource.certified_write',operation_id(request),rid,tx),'certificate_gate')


def install(app):
    op,finish=registration(app,'discussion',('identity','content'))
    fields={k:v for k,v in app.post_fields.items() if k!='parent'}

    @op('discussion.reply',obj({**fields,'target':REF},('target',)))
    async def reply(ctx,request,tx):
        target=decode(ResourceRef,request.arguments['target'])
        original=await tx.resource(target.id)
        require(original.type=='post' and original.state=='active','not_a_post')
        await check_access(app,ctx,request,tx,target.id,'read')
        revision=await tx.revision(target)
        parent=original.parent
        require(tx.setting('policy:'+parent,{}).get('reply_open',True),'replies_closed')
        await check_access(app,ctx,request,tx,parent,'create')
        thread=next((r.target for r in revision.relations if r.type=='thread_root'),ResourceRef(id=original.id,revision=revision.id))
        require((await tx.resource(thread.id)).parent==parent,'cross_thread_reply')
        relations=(Relation(type='reply_to',target=ResourceRef(id=original.id,revision=revision.id)),Relation(type='thread_root',target=thread))
        resource,meta=await create_post(app,ctx,request,tx,parent=parent,relations=relations)
        return output_for(resource,**meta)

    async def quote(ctx,request,tx):
        from msg.plugins.communication import direct_ancestor
        target=decode(ResourceRef,request.arguments['target'])
        await check_access(app,ctx,request,tx,target.id,'read')
        require((await tx.resource(target.id)).type!='legacy_directive',
                'legacy_directive_not_shareable')
        require(await direct_ancestor(tx,target.id) is None,'dm_reference_private')
        source=await tx.revision(target)
        parent=await resolve(tx,request.arguments['parent'])
        await check_access(app,ctx,request,tx,parent,'create')
        kind='quote' if request.operation=='discussion.quote' else 'repost'
        relation=Relation(type=kind,target=ResourceRef(id=target.id,revision=source.id))
        resource,meta=await create_post(app,ctx,request,tx,parent=parent,relations=(relation,))
        return output_for(resource,**meta)
    for name in ('discussion.quote','discussion.repost'):
        op(name,obj({**app.post_fields,'target':REF},('parent','target','body')))(quote)

    async def reaction(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        resource=await tx.resource(rid)
        require(resource.type=='post' and resource.state=='active','not_a_post')
        await check_access(app,ctx,request,tx,rid,'read')
        await gates(app,ctx,request,tx,rid)
        if request.operation=='discussion.like':
            tx.execute("INSERT INTO reactions VALUES (?,?,?,? ,?) ON CONFLICT(subject,resource,kind,revision) DO NOTHING",
                (ctx.principal.subject,rid,'like','',canonical({'actor':ctx.principal.actor,'time':wire(ctx.now)}).decode()),write=True)
        else:
            tx.execute("DELETE FROM reactions WHERE subject=? AND resource=? AND kind='like'",(ctx.principal.subject,rid),write=True)
        count=tx.one("SELECT COUNT(*) FROM reactions WHERE resource=? AND kind='like'",(rid,))[0]
        return HandlerOutput(resources=(ResourceRef(id=rid),),data={'likes':count,'liked':request.operation=='discussion.like'})
    for name in ('discussion.like','discussion.unlike'):
        op(name,obj({'id':IDENTIFIER},('id',)))(reaction)

    @op('discussion.ack',obj({'target':REF,'digest':STRING},('target','digest')))
    async def ack(ctx,request,tx):
        target=decode(ResourceRef,request.arguments['target'])
        require(target.revision is not None,'ack_revision_required')
        await check_access(app,ctx,request,tx,target.id,'read')
        await gates(app,ctx,request,tx,target.id)
        rev=await tx.revision(target)
        require(request.arguments['digest'] in {rev.content.digest,rev.manifest_digest},'ack_digest_mismatch')
        require(ctx.principal.method in {'signature','token'},'ack_signature_or_token_required')
        method=ctx.principal.method
        record={'actor':ctx.principal.actor,'subject':ctx.principal.subject,'auth':method,'revision':rev.id,
                'digest':request.arguments['digest'],'time':wire(ctx.now)}
        if method=='signature':
            record.update(signature=wire(request.proof.signature),signed_envelope=b64(signing_bytes(request)))
        kind='ack.'+method
        if tx.one('SELECT 1 FROM reactions WHERE subject=? AND resource=? AND kind=? AND revision=?',
                  (ctx.principal.subject,target.id,kind,rev.id)) is None:
            from msg.storage.capacity import require_reaction_capacity
            require_reaction_capacity(tx)
        tx.execute('INSERT INTO reactions VALUES (?,?,?,?,?) ON CONFLICT(subject,resource,kind,revision) DO NOTHING',
            (ctx.principal.subject,target.id,kind,rev.id,canonical(record).decode()),write=True)
        return HandlerOutput(resources=(target,),data={'auth':method,'acknowledged_revision':rev.id})

    @op('discussion.acks',obj({'id':IDENTIFIER,'revision':IDENTIFIER,'cursor':STRING,
        'limit':{'type':'integer','minimum':1,'maximum':200}},('id',)),effect='read')
    async def acks(ctx,request,tx):
        from msg.plugins.discovery import next_link
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        filters="resource=? AND kind IN ('ack.signature','ack.token')"
        params=[rid]
        if request.arguments.get('revision'):
            filters+=' AND revision=?'; params.append(request.arguments['revision'])
        binding=digest({'id':rid,'revision':request.arguments.get('revision')})
        position=app.cursors.decode(request.arguments['cursor'],'acks',binding) if request.arguments.get('cursor') else ['','','']
        counts={kind:count for kind,count in tx.rows(f'SELECT kind,COUNT(DISTINCT subject) FROM reactions WHERE {filters} GROUP BY kind',params)}
        limit=request.arguments.get('limit',50)
        rows=tx.rows(f'SELECT body,subject,revision,kind FROM reactions WHERE {filters} AND (subject,revision,kind)>(?,?,?) ORDER BY subject,revision,kind LIMIT ?',
                     (*params,*position,limit+1))
        data={'signed':counts.get('ack.signature',0),'token':counts.get('ack.token',0),
              'items':[loads(row[0]) for row in rows[:limit]]}
        if len(rows)>limit:
            cursor=app.cursors.encode('acks',binding,list(rows[limit-1][1:]))
            data.update(cursor=cursor,next=next_link(app,request.operation,{**request.arguments,'cursor':cursor}),
                        next_requires_auth=ctx.principal.subject is not None)
        return HandlerOutput(data=data)

    @op('discussion.thread',obj({'id':IDENTIFIER,'cursor':STRING,
        'limit':{'type':'integer','minimum':1,'maximum':200}},('id',)),effect='read')
    async def thread(ctx,request,tx):
        from msg.plugins.discovery import next_link
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        revision=await tx.revision(ResourceRef(id=rid))
        root=next((r.target.id for r in revision.relations if r.type=='thread_root'),rid)
        binding=digest({'root':root,'focus':rid})
        position=app.cursors.decode(request.arguments['cursor'],'thread',binding) if request.arguments.get('cursor') else [-1,'','']
        items=[];limit=request.arguments.get('limit',50);more=False
        sql="""SELECT r.id,r.created_at,CASE WHEN r.id=? THEN 0 ELSE 1 END AS priority
            FROM resources r WHERE r.state='active' AND
            (r.id=? OR EXISTS(SELECT 1 FROM relations rel WHERE rel.source_id=r.id
             AND rel.revision_id=r.revision AND rel.type='thread_root' AND rel.target_id=?))
            AND (CASE WHEN r.id=? THEN 0 ELSE 1 END,r.created_at,r.id)>(?,?,?)
            ORDER BY priority,r.created_at,r.id"""
        for id,created_at,priority in tx.execute(sql,(root,root,root,root,*position)):
            if not await visible(app,ctx,request,tx,id):continue
            if len(items)==limit:more=True;break
            items.append(await read_projection(app,ctx,request,tx,id))
            position=[priority,created_at,id]
        data={'root':root,'items':items}
        if more:
            cursor=app.cursors.encode('thread',binding,position)
            data.update(cursor=cursor,next=next_link(app,request.operation,{**request.arguments,'cursor':cursor}),
                        next_requires_auth=ctx.principal.subject is not None)
        # Exact ancestors of a requested reply are references, not duplicated
        # posts/authors/certificate chains. The root has its own normal page item.
        if not request.arguments.get('cursor') and rid!=root:
            ancestors=[];seen={rid};current=revision
            for _ in range(128):
                parent=next((r.target for r in current.relations if r.type=='reply_to'),None)
                if parent is None or parent.id in seen:break
                seen.add(parent.id)
                if not await visible(app,ctx,request,tx,parent.id):break
                ancestors.append(wire(parent))
                if parent.id==root:break
                current=await tx.revision(parent)
            data['ancestors']=ancestors
        return HandlerOutput(data=data)
    finish()
