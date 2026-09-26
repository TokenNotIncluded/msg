"""Reference delivery and durable change feeds, not a workflow engine."""
from __future__ import annotations
from msg.core.codec import wire,canonical,loads,decode,digest
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef,EmailSettings,EffectJob
from msg.plugins.common import *
from msg.plugins.schemas import *
from msg.plugins.discovery import visible


def event_id(request,subject):
    return 'e_'+digest((subject,request.request_id))[7:39]


def install(app):
    op,finish=registration(app,'communication',('identity','content'))

    @op('communication.send',obj({'recipient':IDENTIFIER,'resource':REF},('recipient','resource')))
    async def send(ctx,request,tx):
        recipient=await resolve(tx,request.arguments['recipient'])
        r=await tx.resource(recipient)
        require(r.type in {'user','organization'},'invalid_recipient')
        ref=decode(ResourceRef,request.arguments['resource'])
        await check_access(app,ctx,request,tx,ref.id,'read')
        await app.authorizer._ceiling(ctx.principal,operation_id(request),ref.id,tx)
        revision=await tx.revision(ref)
        ref=ResourceRef(id=ref.id,revision=revision.id)
        record={'id':new_id('message'),'sender':ctx.principal.subject,'actor':ctx.principal.actor,
            'recipient':recipient,'resource':wire(ref),'time':wire(ctx.now),'state':'delivered'}
        eid=event_id(request,ctx.principal.subject)
        tx.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
            (record['id'],ctx.principal.subject,recipient,ref.id,eid,canonical(record).decode()),write=True)
        # Notification is an external projection; it never includes private body content.
        if r.type=='user' and app.settings.server.mail:
            row=tx.one('SELECT body FROM emails WHERE subject=?',(recipient,))
            if row:
                email=decode(EmailSettings,loads(row[0]))
                if email.verified_at and 'communication.send' in email.enabled_events:
                    await tx.enqueue(EffectJob(id=new_id('job'),event_id=eid,kind='mail',
                        dedupe_key=f'{eid}:{recipient}:mail',principal=ctx.principal,operation=request.operation,
                        arguments={'recipient':email.address,'recipient_subject':recipient,'subject':'New msg reference',
                            'text':app.settings.service_url+'/_id/'+ref.id},state='pending',attempts=0,
                        next_attempt_at=ctx.now,lease_until=None))
        return HandlerOutput(resources=(ref,),data={'message_id':record['id'],'recipient':recipient})

    async def watch(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        await app.authorizer._ceiling(ctx.principal,operation_id(request),rid,tx)
        if request.operation=='communication.watch':
            tx.execute('INSERT OR IGNORE INTO watches VALUES (?,?)',(ctx.principal.subject,rid),write=True)
        else:
            tx.execute('DELETE FROM watches WHERE subject=? AND resource=?',(ctx.principal.subject,rid),write=True)
        return HandlerOutput(resources=(ResourceRef(id=rid),),data={'watching':request.operation=='communication.watch'})
    for name in ('communication.watch','communication.unwatch'):
        op(name,obj({'id':IDENTIFIER},('id',)))(watch)

    async def mailbox(ctx,request,tx):
        from msg.plugins.discovery import next_link
        require(ctx.principal.subject is not None,'authentication_required')
        await app.authorizer.require_base(ctx.principal,operation_id(request),ctx.principal.subject,tx)
        inbox=request.operation=='communication.inbox'
        subjects=[ctx.principal.subject]
        if inbox:
            subjects += [m.organization_id for m in await tx.memberships(ctx.principal.subject)]
        binding=digest({'subject':ctx.principal.subject,'groups':sorted(subjects)})
        after=app.cursors.decode(request.arguments['cursor'],request.operation,binding) if request.arguments.get('cursor') else ''
        placeholders=','.join('?' for _ in subjects)
        column='recipient' if inbox else 'sender'
        rows=tx.execute(f'SELECT id,resource,body FROM messages WHERE {column} IN ({placeholders}) AND id>? ORDER BY id',(*subjects,after))
        items=[];limit=request.arguments.get('limit',50);more=False
        for id,rid,raw in rows:
            if await visible(app,ctx,request,tx,rid):
                if len(items)==limit:
                    more=True;break
                items.append(loads(raw));after=id
        data={'items':items}
        if more:
            cursor=app.cursors.encode(request.operation,binding,after)
            data.update(cursor=cursor,next=next_link(app,request.operation,{**request.arguments,'cursor':cursor}),next_requires_auth=True)
        return HandlerOutput(data=data)
    for name in ('communication.inbox','communication.outbox'):
        op(name,obj({'limit':{'type':'integer','minimum':1,'maximum':200},'cursor':STRING}),effect='read')(mailbox)

    @op('communication.changes',obj({'cursor':STRING,'limit':{'type':'integer','minimum':1,'maximum':200}}),effect='read')
    async def changes(ctx,request,tx):
        require(ctx.principal.subject is not None,'authentication_required')
        await app.authorizer.require_base(ctx.principal,operation_id(request),ctx.principal.subject,tx)
        subject=ctx.principal.subject
        cursor=request.arguments.get('cursor')
        watches={r[0] for r in tx.rows('SELECT resource FROM watches WHERE subject=?',(subject,))}
        epoch=tx.setting('authorization_epoch',0)
        watch_digest=digest(sorted(watches))
        saved=app.cursors.decode(cursor,'sync',subject) if cursor else None
        if saved is not None:
            require(isinstance(saved,dict) and saved.get('authorization_epoch')==epoch and
                    saved.get('watch_digest')==watch_digest,'resync_required')
        position=saved['seq'] if saved else tx.setting('sync_floor',0)
        require(type(position) is int and position>=tx.setting('sync_floor',0),'resync_required')
        items=[]
        limit=request.arguments.get('limit',50)
        for seq,raw in tx.execute('SELECT seq,body FROM events WHERE seq>? ORDER BY seq',(position,)):
            position=seq
            event=loads(raw)
            references=event['resources']
            permitted=[]
            for ref in references:
                if await visible(app,ctx,request,tx,ref['id']):
                    permitted.append(ref)
            relevant=event['subject']==subject or event['subject'] in watches
            for ref in permitted:
                relevant=relevant or ref['id'] in watches or any(r.id in watches for r in await tx.ancestors(ref['id']))
            if relevant and (permitted or not references and event['subject']==subject):
                event['resources']=permitted
                items.append({'seq':seq,**event})
            if len(items)>=limit:
                break
        return HandlerOutput(data={'items':items,'sync_cursor':app.cursors.encode('sync',subject,{'seq':position,'authorization_epoch':epoch,'watch_digest':watch_digest})})
    finish()
