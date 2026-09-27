"""Reference delivery and durable change feeds, not a workflow engine."""
from __future__ import annotations
from msg.constants import ROOT_SPACE
from datetime import timedelta
from msg.core.codec import wire,canonical,loads,decode,digest,parse_time,b64
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef,ResourceTypeSpec,EmailSettings,EffectJob
from msg.core.requests import signing_bytes
from msg.plugins.common import *
from msg.plugins.schemas import *
from msg.plugins.discovery import visible


def event_id(request,subject):
    return 'e_'+digest((subject,request.request_id))[7:39]


def _signed_subject(ctx):
    subject=ctx.principal.subject
    require(subject is not None and ctx.principal.actor==subject and ctx.principal.method=='signature',
            'signature_required')
    return subject


def _dm_pair(a,b):
    require(a!=b,'dm_self_request')
    participants=tuple(sorted((a,b)))
    return digest(participants),participants


def _dm_record(tx,conversation_id,subject):
    row=tx.one('SELECT pair,participant_a,participant_b,initiator,state FROM dm_conversations WHERE resource_id=?',
               (conversation_id,))
    require(row is not None and subject in row[1:3],'dm_not_found')
    return row


async def direct_ancestor(tx,rid):
    chain=(*await tx.ancestors(rid),await tx.resource(rid))
    for resource in chain:
        if tx.one('SELECT 1 FROM dm_conversations WHERE resource_id=?',(resource.id,)):
            return resource.id
    return None


def _dm_notice(tx,ctx,request,recipient,resource):
    record={'id':new_id('message'),'sender':ctx.principal.subject,'actor':ctx.principal.actor,
            'recipient':recipient,'resource':wire(ResourceRef(id=resource)),
            'time':wire(ctx.now),'state':'delivered','source':'dm'}
    tx.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
               (record['id'],ctx.principal.subject,recipient,resource,
                event_id(request,ctx.principal.subject),canonical(record).decode()),write=True)


def install(app):
    op,finish=registration(app,'communication',('identity','content'))

    @op('communication.presence_get',obj({'subject_id':IDENTIFIER},('subject_id',)),effect='read')
    async def presence_get(ctx,request,tx):
        subject=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(subject)
        row=tx.one('SELECT expires_at,body FROM presence WHERE subject=?',(subject,))
        if row is None or parse_time(row[0])<=ctx.now:
            return HandlerOutput(data={'subject_id':subject,'state':'unknown'})
        return HandlerOutput(data=loads(row[1]))

    @op('communication.presence_set',obj({
        'state':{'enum':['available','busy','away']},
        'message':{'type':'string','maxLength':240},
        'capabilities_hint':{'type':'array','items':{'type':'string','maxLength':80},
                             'maxItems':12,'uniqueItems':True},
        'ttl':{'type':'integer','minimum':30,'maximum':3600}},('state',)),signature=True)
    async def presence_set(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        args=request.arguments
        record={'subject_id':subject,'state':args['state'],'updated_at':wire(ctx.now),
                'expires_at':wire(ctx.now+timedelta(seconds=args.get('ttl',300))),
                'self_reported':True}
        if 'message' in args:
            record['message']=args['message']
        if 'capabilities_hint' in args:
            record['capabilities_hint']=args['capabilities_hint']
        tx.execute('''INSERT INTO presence (subject,expires_at,body) VALUES (?,?,?)
            ON CONFLICT(subject) DO UPDATE SET expires_at=excluded.expires_at,body=excluded.body''',
            (subject,record['expires_at'],canonical(record).decode()),write=True)
        return HandlerOutput(data=record)

    @op('communication.presence_clear',obj(),signature=True)
    async def presence_clear(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        tx.execute('DELETE FROM presence WHERE subject=?',(subject,),write=True)
        return HandlerOutput(data={'subject_id':subject,'state':'unknown'})

    claim_value={'type':['object','array','string','integer','number','boolean','null']}
    evidence_schema={'type':'array','items':REF,'maxItems':16}

    @op('communication.claim_create',obj({
        'predicate':{'type':'string','minLength':1,'maxLength':120},
        'value':claim_value,'expires_at':STRING,'evidence_refs':evidence_schema,
    },('predicate','value')),signature=True)
    async def claim_create(ctx,request,tx):
        from msg.plugins.common import create_resource
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        args=request.arguments
        require(len(canonical(args))<=8192,'claim_too_large')
        expires=args.get('expires_at')
        if expires is not None:
            ending=parse_time(expires)
            require(ctx.now<ending<=ctx.now+timedelta(days=365),'claim_expiry_invalid')
        refs=[]
        for raw in args.get('evidence_refs',()):
            ref=decode(ResourceRef,raw)
            rid=await resolve(tx,ref.id)
            await check_access(app,ctx,request,tx,rid,'read')
            if ref.revision is not None:
                await tx.revision(ResourceRef(id=rid,revision=ref.revision))
            refs.append(ResourceRef(id=rid,revision=ref.revision))
        try:
            parent=await tx.resolve((await tx.path(subject))+'/claims')
            folder=await tx.resource(parent)
            require(folder.type=='topic' and folder.parent==subject and folder.owner==subject and
                    not folder.mode&0o002,'claim_folder_conflict')
        except Failure as exc:
            if exc.code!='not_found':
                raise
            folder=await create_resource(app,ctx,request,tx,parent=subject,type='topic',
                                         name='claims',mode=0o755)
            parent=folder.id
        rid=new_id('claim')
        assertion={'kind':'self_claim','subject_id':subject,'predicate':args['predicate'],
                   'value':args['value'],'issued_at':wire(ctx.now),'expires_at':expires,
                   'evidence_digest':digest(wire(refs)),'authority':'none'}
        resource=await create_resource(app,ctx,request,tx,parent=parent,type='claim',name=rid,
                                       body=canonical(assertion),media_type='application/json',
                                       resource_id=rid,mode=0o444)
        record={**assertion,'id':rid,'evidence_refs':wire(refs),
                'signature':wire(request.proof.signature),
                'signed_envelope':b64(signing_bytes(request))}
        tx.execute('INSERT INTO claims (id,subject,issued_at,expires_at,body) VALUES (?,?,?,?,?)',
                   (rid,subject,assertion['issued_at'],expires,canonical(record).decode()),write=True)
        return HandlerOutput(resources=(ResourceRef(id=rid,revision=resource.revision),),
                             data={'claim_id':rid,'kind':'self_claim','authority':'none'})

    @op('communication.claim_get',obj({'id':IDENTIFIER},('id',)),effect='read')
    async def claim_get(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        row=tx.one('SELECT body FROM claims WHERE id=?',(rid,))
        require(row is not None,'claim_not_found')
        await check_access(app,ctx,request,tx,rid,'read')
        record=dict(loads(row[0]))
        refs=[]
        for raw in record.pop('evidence_refs'):
            try:
                if await visible(app,ctx,request,tx,raw['id']):
                    refs.append(raw)
            except Failure as exc:
                if exc.code!='not_found':
                    raise
        envelope=record.pop('signed_envelope')
        record['evidence_refs']=refs
        if len(refs)==len(loads(row[0])['evidence_refs']):
            record['signed_envelope']=envelope
        return HandlerOutput(data=record)

    @op('communication.claim_list',obj({'subject_id':IDENTIFIER},('subject_id',)),effect='read')
    async def claim_list(ctx,request,tx):
        subject=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(subject)
        rows=tx.rows('SELECT body FROM claims WHERE subject=? ORDER BY issued_at,id',(subject,))
        items=[]
        for (raw,) in rows:
            claim=loads(raw)
            if await visible(app,ctx,request,tx,claim['id']):
                items.append({key:claim[key] for key in ('id','kind','subject_id','predicate','value',
                              'issued_at','expires_at','evidence_digest','authority')})
        return HandlerOutput(data={'items':items})

    @op('communication.dm_request',obj({'recipient':IDENTIFIER},('recipient',)),signature=True)
    async def dm_request(ctx,request,tx):
        from msg.plugins.common import create_resource
        sender=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),sender,tx)
        recipient=await resolve(tx,request.arguments['recipient'])
        target=await tx.resource(recipient)
        require(target.type=='user' and target.state=='active','invalid_recipient')
        await tx.subject(recipient)
        pair,(first,second)=_dm_pair(sender,recipient)
        require(tx.one('SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
                       (sender,recipient,recipient,sender)) is None,'dm_blocked')
        row=tx.one('SELECT resource_id,state FROM dm_conversations WHERE pair=?',(pair,))
        if row is not None:
            require(row[1]!='rejected','dm_rejected')
            return HandlerOutput(resources=(ResourceRef(id=row[0]),),
                                 data={'conversation_id':row[0],'state':row[1],'participant_pair':[first,second]})
        topic=await create_resource(app,ctx,request,tx,parent=ROOT_SPACE,type='topic',
                                    name='dm-'+new_id('c'),mode=0o700)
        tx.set_setting('policy:'+topic.id,{'post_mode':'0600','editable':True})
        tx.execute('''INSERT INTO dm_conversations
            (pair,resource_id,participant_a,participant_b,initiator,state,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)''',
                   (pair,topic.id,first,second,sender,'pending',wire(ctx.now),wire(ctx.now)),write=True)
        _dm_notice(tx,ctx,request,recipient,topic.id)
        return HandlerOutput(resources=(ResourceRef(id=topic.id),),
                             data={'conversation_id':topic.id,'state':'pending','participant_pair':[first,second]})

    async def dm_decision(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        rid=await resolve(tx,request.arguments['conversation_id'])
        pair,first,second,initiator,state=_dm_record(tx,rid,subject)
        require(subject!=initiator,'dm_recipient_required')
        desired='active' if request.operation=='communication.dm_accept' else 'rejected'
        require(state=='pending' or state==desired,'dm_request_closed')
        if state=='pending':
            require(tx.one('SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
                           (first,second,second,first)) is None,'dm_blocked')
            tx.execute('UPDATE dm_conversations SET state=?,updated_at=? WHERE pair=? AND state=?',
                       (desired,wire(ctx.now),pair,'pending'),write=True)
            _dm_notice(tx,ctx,request,initiator,rid)
        return HandlerOutput(resources=(ResourceRef(id=rid),),
                             data={'conversation_id':rid,'state':desired})
    for name in ('communication.dm_accept','communication.dm_reject'):
        op(name,obj({'conversation_id':IDENTIFIER},('conversation_id',)),signature=True)(dm_decision)

    @op('communication.dm_send',obj({'conversation_id':IDENTIFIER,'body':STRING},
                                    ('conversation_id','body')),signature=True)
    async def dm_send(ctx,request,tx):
        from msg.plugins.content import create_post
        subject=_signed_subject(ctx)
        rid=await resolve(tx,request.arguments['conversation_id'])
        pair,first,second,initiator,state=_dm_record(tx,rid,subject)
        await check_access(app,ctx,request,tx,rid,'create')
        require(state=='active','dm_not_active')
        post,_=await create_post(app,ctx,request,tx,parent=rid)
        recipient=second if subject==first else first
        _dm_notice(tx,ctx,request,recipient,post.id)
        return output_for(post,conversation_id=rid)

    @op('communication.dm_list',obj(),effect='read')
    async def dm_list(ctx,request,tx):
        subject=ctx.principal.subject
        require(subject is not None,'authentication_required')
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        rows=tx.rows('''SELECT d.resource_id,d.participant_a,d.participant_b,d.state,d.initiator
            FROM dm_conversations d LEFT JOIN dm_archives a ON a.pair=d.pair AND a.subject=?
            WHERE (d.participant_a=? OR d.participant_b=?) AND a.subject IS NULL ORDER BY d.created_at,d.resource_id''',
            (subject,subject,subject))
        return HandlerOutput(data={'items':[{'conversation_id':rid,'other_subject':b if subject==a else a,
             'state':state,'initiator':initiator} for rid,a,b,state,initiator in rows]})

    @op('communication.dm_archive',obj({'conversation_id':IDENTIFIER},('conversation_id',)),signature=True)
    async def dm_archive(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        rid=await resolve(tx,request.arguments['conversation_id'])
        pair,*_=_dm_record(tx,rid,subject)
        tx.execute('INSERT INTO dm_archives (subject,pair) VALUES (?,?) ON CONFLICT(subject,pair) DO NOTHING',
                   (subject,pair),write=True)
        return HandlerOutput(data={'conversation_id':rid,'archived':True})

    @op('communication.dm_block',obj({'subject_id':IDENTIFIER},('subject_id',)),signature=True)
    async def dm_block(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        other=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(other)
        require(other!=subject,'dm_self_request')
        tx.execute('INSERT INTO dm_blocks (blocker,blocked) VALUES (?,?) ON CONFLICT(blocker,blocked) DO NOTHING',
                   (subject,other),write=True)
        return HandlerOutput(data={'subject_id':other,'blocked':True})

    @op('communication.send',obj({'recipient':IDENTIFIER,'resource':REF},('recipient','resource')))
    async def send(ctx,request,tx):
        recipient=await resolve(tx,request.arguments['recipient'])
        r=await tx.resource(recipient)
        require(r.type in {'user','organization'},'invalid_recipient')
        ref=decode(ResourceRef,request.arguments['resource'])
        await check_access(app,ctx,request,tx,ref.id,'read')
        require(await direct_ancestor(tx,ref.id) is None,'dm_reference_private')
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
                direct=await direct_ancestor(tx,ref['id'])
                if direct is not None:
                    relevant=True
            if relevant and (permitted or not references and event['subject']==subject):
                event['resources']=permitted
                items.append({'seq':seq,**event})
            if len(items)>=limit:
                break
        return HandlerOutput(data={'items':items,'sync_cursor':app.cursors.encode('sync',subject,{'seq':position,'authorization_epoch':epoch,'watch_digest':watch_digest})})
    finish((ResourceTypeSpec(name='claim',version=1,container=False,content_schema=None,
                             operations=frozenset(),relations=frozenset()),))
