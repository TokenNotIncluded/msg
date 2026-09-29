"""Reference delivery and durable change feeds, not a workflow engine."""
from __future__ import annotations
import hmac
from dataclasses import replace
from datetime import UTC,datetime
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from msg.constants import ROOT_SPACE
from datetime import timedelta
from msg.core.codec import wire,canonical,loads,decode,digest,parse_time,b64,unb64
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef,ResourceTypeSpec,EmailSettings,EffectJob,Principal
from msg.core.requests import signing_bytes
from msg.plugins.common import *
from msg.plugins.schemas import *
from msg.plugins.discovery import visible
from msg.storage.capacity import require_webhook_capacity


from msg.core.events import event_id


# A deliberately small event vocabulary. An Event is an audit fact, not blanket
# permission to disclose its data to an external receiver.
WEBHOOK_DOMAIN_EVENTS = {
    'content.post_create': 'resource.created',
    'discussion.reply': 'resource.created',
    'content.post_edit': 'resource.updated',
}


def _webhook_subscription_key(subject,resource_id):
    return 'webhook_subscription:'+subject+':'+resource_id


async def enqueue_domain_webhooks(app,tx,event):
    """Project an explicit owner's Event subscription inside the Event transaction."""
    category=WEBHOOK_DOMAIN_EVENTS.get(event.type)
    if category is None:
        return
    for ref in event.resources:
        resource=await tx.resource(ref.id)
        for scope in (resource, await tx.resource(resource.parent) if resource.parent else None):
            if scope is None:
                continue
            owner=scope.owner
            subscription=tx.setting(_webhook_subscription_key(owner,scope.id))
            if not subscription or not subscription.get('enabled') or category not in subscription.get('events',()):
                continue
            endpoint=tx.one('SELECT enabled,generation FROM webhook_endpoints WHERE subject=?',(owner,))
            if not endpoint or endpoint[0]!=1 or endpoint[1]!=subscription['endpoint_generation']:
                continue
            principal=decode(Principal,subscription['principal'])
            # It must remain an owner-controlled subscription, not a delegation
            # that can be retargeted by an event emitted from another account.
            if principal.subject!=owner or principal.actor!=owner or principal.method!='signature':
                continue
            require_webhook_capacity(tx)
            await tx.enqueue(EffectJob(id=new_id('job'),event_id=event.id,kind='webhook',
                dedupe_key=f'{event.id}:{owner}:{scope.id}:{ref.id}:{category}:webhook',
                principal=principal,operation='communication.webhook_subscribe',
                arguments={'recipient_subject':owner,'resource_id':ref.id,'scope_id':scope.id,
                           'category':category,'endpoint_generation':endpoint[1],
                           'subscription_generation':subscription['generation']},
                state='pending',attempts=0,next_attempt_at=event.time,lease_until=None))


def sync_seen_context(subject,sequence,expires_at):
    return canonical({'subject':subject,'seq':sequence,'expires_at':expires_at})


def seal_sync_seen(app,seen,context=b''):
    require(len(seen)<=64,'resync_required')
    raw=canonical(seen)
    require(len(raw)<=4096,'resync_required')
    key=hmac.digest(app.cursors.key,b'msg-sync-seen-v1','sha256')
    nonce_key=hmac.digest(app.cursors.key,b'msg-sync-seen-nonce-v1','sha256')
    nonce=hmac.digest(nonce_key,context+b'\0'+raw,'sha256')[:12]
    return b64(nonce+AESGCM(key).encrypt(nonce,raw,context))


def open_sync_seen(app,value,context=b''):
    payload=unb64(value,limit=8192)
    require(28<=len(payload)<=4124,'resync_required')
    key=hmac.digest(app.cursors.key,b'msg-sync-seen-v1','sha256')
    try:
        seen=loads(AESGCM(key).decrypt(payload[:12],payload[12:],context))
    except InvalidTag as exc:
        raise Failure('invalid_cursor') from exc
    require(type(seen) is list and len(seen)<=64 and
            all(type(rid) is str and len(rid)<=160 for rid in seen) and
            len(set(seen))==len(seen),'resync_required')
    return seen


def sync_checkpoint_token(app,subject,checkpoint_id,version):
    return app.cursors.encode('sync-checkpoint',{'subject':subject},
                              {'id':checkpoint_id,'version':version})


def sync_checkpoint_record(app,tx,ctx,token):
    saved=app.cursors.inspect(token)
    require(saved.get('kind')=='sync-checkpoint','cursor_kind_mismatch')
    subject=ctx.principal.subject
    require(saved.get('query')=={'subject':subject},'cursor_principal_mismatch')
    position=saved.get('position')
    require(isinstance(position,dict) and type(position.get('version')) is int and
            isinstance(position.get('id'),str),'invalid_cursor')
    row=tx.one('SELECT subject,credential_id,version,expires_at,body FROM sync_checkpoints WHERE id=?',
               (position['id'],))
    require(row is not None,'resync_required')
    require(row[0]==subject and row[1]==ctx.principal.credential_id and
            ctx.principal.actor==subject and ctx.principal.method=='signature',
            'cursor_principal_mismatch')
    require(row[2]==position['version'],'checkpoint_conflict')
    require(ctx.now<parse_time(row[3]),'resync_required')
    body=loads(row[4])
    require(type(body.get('seq')) is int and type(body.get('seen')) is list and
            len(body['seen'])<=10000 and len(set(body['seen']))==len(body['seen']) and
            all(type(rid) is str and len(rid)<=160 for rid in body['seen']),
            'resync_required')
    require(body['seq']>=tx.setting('sync_floor',0),'resync_required')
    return position['id'],row[2],body


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

    @op('communication.webhook_set',obj({'url':{'type':'string','minLength':1,'maxLength':2048},
        'secret':{'type':'string','minLength':43,'maxLength':86}},('url','secret')),signature=True)
    async def webhook_set(ctx,request,tx):
        from msg.workers.webhook import seal_secret, validate_endpoint
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        url=request.arguments['url']
        validate_endpoint(url)
        nonce,ciphertext=seal_secret(app,subject,request.arguments['secret'])
        tx.execute('''INSERT INTO webhook_endpoints (subject,url,nonce,ciphertext,enabled,generation)
            VALUES (?,?,?,?,1,1) ON CONFLICT(subject) DO UPDATE SET url=excluded.url,
            nonce=excluded.nonce,ciphertext=excluded.ciphertext,enabled=1,
            generation=webhook_endpoints.generation+1''',
            (subject,url,nonce,ciphertext),write=True)
        return HandlerOutput(data={'enabled':True,'url':url})

    @op('communication.webhook_disable',obj(),signature=True)
    async def webhook_disable(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        tx.execute('UPDATE webhook_endpoints SET enabled=0,generation=generation+1 WHERE subject=?',
                   (subject,),write=True)
        return HandlerOutput(data={'enabled':False})

    @op('communication.webhook_status',obj(),effect='read')
    async def webhook_status(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        row=tx.one('SELECT url,enabled,generation FROM webhook_endpoints WHERE subject=?',(subject,))
        return HandlerOutput(data={'enabled':bool(row and row[1]),'url':row[0] if row else None,
                                   'generation':row[2] if row else 0})

    @op('communication.webhook_subscribe',obj({
        'resource_id':IDENTIFIER,
        'events':{'type':'array','items':{'enum':sorted(set(WEBHOOK_DOMAIN_EVENTS.values()))},
                  'minItems':1,'maxItems':2,'uniqueItems':True}},('resource_id','events')),signature=True)
    async def webhook_subscribe(ctx,request,tx):
        owner=_signed_subject(ctx)
        rid=await resolve(tx,request.arguments['resource_id'])
        resource=await tx.resource(rid)
        require(resource.owner==owner and resource.state=='active','webhook_scope_not_owned')
        await check_access(app,ctx,request,tx,rid,'manage')
        require(await app.authorizer.has(ctx.principal,'webhook.domain',operation_id(request),rid,tx),
                'capability_required')
        row=tx.one('SELECT enabled,generation FROM webhook_endpoints WHERE subject=?',(owner,))
        require(row is not None and row[0]==1,'webhook_disabled')
        key=_webhook_subscription_key(owner,rid)
        previous=tx.setting(key,{})
        events=sorted(request.arguments['events'])
        generation=previous.get('generation',0)+1
        tx.set_setting(key,{'enabled':True,'events':events,'endpoint_generation':row[1],
                            'generation':generation,'principal':wire(ctx.principal)})
        return HandlerOutput(data={'resource_id':rid,'events':events,'enabled':True,
                                   'generation':generation})

    @op('communication.webhook_unsubscribe',obj({'resource_id':IDENTIFIER},('resource_id',)),signature=True)
    async def webhook_unsubscribe(ctx,request,tx):
        owner=_signed_subject(ctx)
        rid=await resolve(tx,request.arguments['resource_id'])
        key=_webhook_subscription_key(owner,rid)
        previous=tx.setting(key,{})
        # Removal is always allowed by the signing owner even after a resource
        # is archived or permissions changed; pending deliveries must stop.
        tx.set_setting(key,{'enabled':False,'events':[],
                            'generation':previous.get('generation',0)+1})
        return HandlerOutput(data={'resource_id':rid,'enabled':False})

    @op('communication.webhook_subscription',obj({'resource_id':IDENTIFIER},('resource_id',)),effect='read')
    async def webhook_subscription(ctx,request,tx):
        owner=_signed_subject(ctx)
        rid=await resolve(tx,request.arguments['resource_id'])
        record=tx.setting(_webhook_subscription_key(owner,rid),{})
        return HandlerOutput(data={'resource_id':rid,'enabled':bool(record.get('enabled')),
                                   'events':record.get('events',[]),
                                   'generation':record.get('generation',0)})

    @op('communication.presence_get',obj({'subject_id':IDENTIFIER},('subject_id',)),effect='read')
    async def presence_get(ctx,request,tx):
        subject=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(subject)
        await check_access(app,ctx,request,tx,subject,'read')
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
        introduction=request.arguments.get('introduction')
        intro_ref=None
        if introduction is not None:
            require(len(introduction.encode('utf-8'))<=1024,'introduction_too_large')
            intro=await create_resource(app,ctx,request,tx,parent=topic.id,type='post',
                                        body=introduction,media_type='text/markdown',mode=0o600)
            intro_ref=ResourceRef(id=intro.id,revision=intro.revision)
        _dm_notice(tx,ctx,request,recipient,topic.id)
        data={'conversation_id':topic.id,'state':'pending',
              'participant_pair':[first,second]}
        if intro_ref is not None:
            data['introduction_ref']=wire(intro_ref)
        return HandlerOutput(resources=(ResourceRef(id=topic.id),),data=data)
    op('communication.dm_request',obj({'recipient':IDENTIFIER},('recipient',)),
       signature=True)(dm_request)
    op('communication.dm_request',obj({'recipient':IDENTIFIER,
        'introduction':{'type':'string','minLength':1,'maxLength':500}},
        ('recipient',)),signature=True,version=2)(dm_request)

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
        require((await tx.resource(ref.id)).type!='legacy_directive',
                'legacy_directive_not_shareable')
        require(await direct_ancestor(tx,ref.id) is None,'dm_reference_private')
        await app.authorizer._ceiling(ctx.principal,operation_id(request),ref.id,tx)
        revision=await tx.revision(ref)
        ref=ResourceRef(id=ref.id,revision=revision.id)
        record={'id':new_id('message'),'sender':ctx.principal.subject,'actor':ctx.principal.actor,
            'recipient':recipient,'resource':wire(ref),'time':wire(ctx.now),'state':'delivered'}
        eid=event_id(request,ctx.principal.subject)
        tx.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
            (record['id'],ctx.principal.subject,recipient,ref.id,eid,canonical(record).decode()),write=True)
        if r.type=='user':
            row=tx.one('SELECT enabled,generation FROM webhook_endpoints WHERE subject=?',(recipient,))
            if row and row[0]:
                require_webhook_capacity(tx)
                await tx.enqueue(EffectJob(id=new_id('job'),event_id=eid,kind='webhook',
                    dedupe_key=f'{eid}:{recipient}:webhook',principal=ctx.principal,
                    operation=request.operation,arguments={'recipient_subject':recipient,
                        'message_id':record['id'],'endpoint_generation':row[1]},
                    state='pending',attempts=0,next_attempt_at=ctx.now,lease_until=None))
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
            if event['type'].startswith('topic.') and event.get('data',{}).get('reason'):
                from msg.plugins.content import topic_admin
                topic=event['data'].get('topic_id')
                if topic is None or not topic_admin(tx,topic,subject):
                    event['data']=dict(event['data'])
                    event['data'].pop('reason',None)
            references=event['resources']
            permitted=[]
            for ref in references:
                if await visible(app,ctx,request,tx,ref['id']):
                    permitted.append(ref)
            relevant=event['subject']==subject or event['subject'] in watches
            if event['type'].startswith('topic.'):
                topic=event.get('data',{}).get('topic_id')
                member=tx.one("SELECT 1 FROM topic_memberships WHERE topic=? AND subject=? AND status='active'",
                              (topic,subject)) if topic else None
                relevant=relevant or member is not None
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

    def sync_authority(tx,subject):
        watches={row[0] for row in tx.rows('SELECT resource FROM watches WHERE subject=?',(subject,))}
        return (watches,digest(sorted(watches)),tx.setting('authorization_epoch',0),
                digest([tuple(row) for row in tx.rows(
                    'SELECT topic,role,status FROM topic_memberships WHERE subject=? ORDER BY topic',(subject,))]))

    @op('communication.sync_checkpoint_open',obj({'cursor':STRING},('cursor',)),signature=True)
    async def sync_checkpoint_open(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        saved=app.cursors.inspect(request.arguments['cursor'])
        require(saved.get('kind')=='sync-v2' and saved.get('query')=={'subject':subject},
                'cursor_kind_mismatch')
        position=saved.get('position')
        require(position.get('principal')=={'actor':subject,'subject':subject,
                'credential_id':ctx.principal.credential_id},'cursor_principal_mismatch')
        watches,watch_hash,epoch,topic_hash=sync_authority(tx,subject)
        require(position.get('watch_digest')==watch_hash and
                position.get('authorization_epoch')==epoch and
                position.get('topic_membership_digest')==topic_hash and
                ctx.now<parse_time(position['expires_at']) and
                type(position.get('seq')) is int and
                position['seq']>=tx.setting('sync_floor',0),'resync_required')
        seen=open_sync_seen(app,position.get('seen_ciphertext',''),
                            sync_seen_context(subject,position['seq'],position['expires_at']))
        checkpoint_id=new_id('sync')
        expires_at=wire(ctx.now+timedelta(days=30))
        body={'seq':position['seq'],'seen':seen,'watch_digest':watch_hash,
              'authorization_epoch':epoch,'topic_membership_digest':topic_hash}
        tx.execute('INSERT INTO sync_checkpoints VALUES (?,?,?,?,?,?)',
                   (checkpoint_id,subject,ctx.principal.credential_id,0,expires_at,
                    canonical(body).decode()),write=True)
        cursor=sync_checkpoint_token(app,subject,checkpoint_id,0)
        return HandlerOutput(data={'sync_cursor':cursor,'next':'/_r/s/'+cursor,
                                   'next_requires_auth':True,'expires_at':expires_at})

    async def checkpoint_delta(ctx,request,tx,body,limit):
        subject=ctx.principal.subject
        # ACK must recompute precisely the same visibility decision as GET.
        request=replace(request,operation='communication.sync')
        watches,watch_hash,epoch,topic_hash=sync_authority(tx,subject)
        require(body['watch_digest']==watch_hash,'resync_required')
        stale=(body['authorization_epoch']!=epoch or
               body['topic_membership_digest']!=topic_hash)
        sequence=body['seq']
        seen=list(body['seen'])
        items=[]
        async def can_read(rid):
            try:
                return await visible(app,ctx,request,tx,rid)
            except Failure as exc:
                if exc.code in {'not_found','resource_purged','ancestor_inactive'}:
                    return False
                raise
        for rid in tuple(seen):
            if not await can_read(rid):
                items.append({'kind':'revoked','ref':{'id':rid}})
                seen.remove(rid)
                if len(items)>=limit:
                    break
        if stale:
            # Known losses can be drained over several acknowledged pages. A
            # permission gain may expose older events, so no event sequence is
            # advanced until the client rebuilds a fresh visible baseline.
            if not items:
                raise Failure('resync_required')
        elif len(items)<limit:
            scanned=0
            for seq,raw in tx.execute('SELECT seq,body FROM events WHERE seq>? ORDER BY seq',(sequence,)):
                scanned+=1
                require(scanned<=5000,'resync_required')
                event=loads(raw)
                candidates=[]
                for ref in event['resources']:
                    rid=ref['id']
                    if not await can_read(rid):
                        continue
                    relevant=(event['subject']==subject or rid in watches or
                              any(ancestor.id in watches for ancestor in await tx.ancestors(rid)))
                    if not relevant:
                        direct=await direct_ancestor(tx,rid)
                        if direct is not None:
                            pair=tx.one('SELECT participant_a,participant_b FROM dm_conversations '
                                        'WHERE resource_id=?',(direct,))
                            relevant=pair is not None and subject in pair
                    if not relevant:
                        continue
                    kind=('archived' if event['type'] in {'content.archive','content.purge'} else
                          'created' if event['type'] in {'content.post_create','content.topic_create',
                                                        'discussion.reply','discussion.quote'} else 'modified')
                    candidates.append({'seq':seq,'kind':kind,'event_type':event['type'],'ref':ref})
                require(len(candidates)<=limit,'resync_required')
                if len(items)+len(candidates)>limit:
                    break
                for item in candidates:
                    rid=item['ref']['id']
                    if rid not in seen:
                        require(len(seen)<10000,'resync_required')
                        seen.append(rid)
                items.extend(candidates)
                sequence=seq
        return items,{**body,'seq':sequence,'seen':seen},stale

    @op('communication.sync_checkpoint_ack',obj({'ack':STRING},('ack',)),signature=True)
    async def sync_checkpoint_ack(ctx,request,tx):
        subject=_signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        proof=app.cursors.inspect(request.arguments['ack'])
        require(proof.get('kind')=='sync-checkpoint-ack' and
                proof.get('query')=={'subject':subject},'invalid_cursor')
        position=proof.get('position')
        require(isinstance(position,dict) and type(position.get('limit')) is int and
                1<=position['limit']<=100,'invalid_cursor')
        current=sync_checkpoint_token(app,subject,position['id'],position['version'])
        checkpoint_id,version,body=sync_checkpoint_record(app,tx,ctx,current)
        items,updated,stale=await checkpoint_delta(ctx,request,tx,body,position['limit'])
        require(digest({'items':items,'state':updated,'stale':stale})==position['digest'],
                'checkpoint_stale')
        expires_at=wire(ctx.now+timedelta(days=30))
        result=tx.execute('UPDATE sync_checkpoints SET version=?,expires_at=?,body=? WHERE id=? AND version=?',
                          (version+1,expires_at,canonical(updated).decode(),checkpoint_id,version),write=True)
        require(result.rowcount==1,'checkpoint_conflict')
        cursor=sync_checkpoint_token(app,subject,checkpoint_id,version+1)
        return HandlerOutput(data={'sync_cursor':cursor,'next':'/_r/s/'+cursor,
                                   'next_requires_auth':True,'resync_required':stale,
                                   'expires_at':expires_at})

    @op('communication.sync',obj({'cursor':STRING,
        'limit':{'type':'integer','minimum':1,'maximum':100}}),effect='read')
    async def sync(ctx,request,tx):
        subject=ctx.principal.subject
        require(subject is not None,'authentication_required')
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        if request.arguments.get('cursor'):
            kind=app.cursors.inspect(request.arguments['cursor']).get('kind')
            if kind=='sync-checkpoint':
                token=request.arguments['cursor']
                checkpoint_id,version,body=sync_checkpoint_record(app,tx,ctx,token)
                limit=request.arguments.get('limit',50)
                items,updated,stale=await checkpoint_delta(ctx,request,tx,body,limit)
                ack=app.cursors.encode('sync-checkpoint-ack',{'subject':subject},
                    {'id':checkpoint_id,'version':version,'limit':limit,
                     'digest':digest({'items':items,'state':updated,'stale':stale})})
                return HandlerOutput(data={'items':items,'sync_cursor':token,
                    'checkpoint_ack':ack,'resync_required':stale})
        principal={'actor':ctx.principal.actor,'subject':subject,
                   'credential_id':ctx.principal.credential_id}
        watches={row[0] for row in tx.rows('SELECT resource FROM watches WHERE subject=?',(subject,))}
        watch_hash=digest(sorted(watches))
        authorization_epoch=tx.setting('authorization_epoch',0)
        topic_membership_hash=digest([tuple(row) for row in tx.rows(
            'SELECT topic,role,status FROM topic_memberships WHERE subject=? ORDER BY topic',(subject,))])
        floor=tx.setting('sync_floor',0)
        if request.arguments.get('cursor'):
            saved=app.cursors.inspect(request.arguments['cursor'])
            require(saved.get('kind')=='sync-v2','cursor_kind_mismatch')
            require(saved.get('query')=={'subject':subject},'cursor_principal_mismatch')
            position=saved['position']
            require(position.get('principal')==principal,'cursor_principal_mismatch')
            require(position.get('watch_digest')==watch_hash,'resync_required')
            require(ctx.now<parse_time(position['expires_at']),'resync_required')
            require(type(position.get('seq')) is int and position['seq']>=floor,
                    'resync_required')
            sequence=position['seq']
            context=sync_seen_context(subject,position['seq'],position['expires_at'])
            seen=open_sync_seen(app,position.get('seen_ciphertext',''),context)
            expires_at=parse_time(position['expires_at'])
        else:
            sequence=floor
            seen=[]
            minute=int(ctx.now.timestamp()//60)
            expires_at=datetime.fromtimestamp((minute+15)*60,UTC)
        limit=request.arguments.get('limit',50)
        items=[]
        async def can_read(rid):
            try:
                return await visible(app,ctx,request,tx,rid)
            except Failure as exc:
                if exc.code in {'not_found','resource_purged','ancestor_inactive'}:
                    return False
                raise
        if request.arguments.get('cursor') and (
            position.get('authorization_epoch')!=authorization_epoch or
            position.get('topic_membership_digest')!=topic_membership_hash
        ):
            # A permission gain can expose events before the saved sequence.  A
            # cursor cannot prove that replay is complete, so never advance it.
            # Known losses may still be reported without exposing event content.
            revoked=[{'kind':'revoked','ref':{'id':rid}} for rid in seen
                     if not await can_read(rid)]
            if revoked:
                return HandlerOutput(data={'items':revoked[:limit],
                                           'resync_required':True})
            raise Failure('resync_required')
        for rid in tuple(seen):
            if not await can_read(rid):
                if len(items)==limit:break
                items.append({'kind':'revoked','ref':{'id':rid}})
                seen.remove(rid)
        if len(items)<limit:
            scanned=0
            for seq,raw in tx.execute('SELECT seq,body FROM events WHERE seq>? ORDER BY seq',(sequence,)):
                scanned+=1
                require(scanned<=5000,'resync_required')
                event=loads(raw)
                candidates=[]
                for ref in event['resources']:
                    rid=ref['id']
                    if not await can_read(rid):
                        continue
                    relevant=(event['subject']==subject or rid in watches or
                              any(ancestor.id in watches for ancestor in await tx.ancestors(rid)))
                    if not relevant:
                        direct=await direct_ancestor(tx,rid)
                        if direct is not None:
                            pair=tx.one('SELECT participant_a,participant_b FROM dm_conversations '
                                        'WHERE resource_id=?',(direct,))
                            relevant=pair is not None and subject in pair
                    if not relevant:
                        continue
                    kind=('archived' if event['type'] in {'content.archive','content.purge'} else
                          'created' if event['type'] in {'content.post_create','content.topic_create',
                                                        'discussion.reply','discussion.quote'} else 'modified')
                    candidates.append({'seq':seq,'kind':kind,'event_type':event['type'],
                                       'ref':ref})
                require(len(candidates)<=limit,'resync_required')
                if len(items)+len(candidates)>limit:
                    break
                for item in candidates:
                    rid=item['ref']['id']
                    if rid not in seen:
                        require(len(seen)<64,'resync_required')
                        seen.append(rid)
                items.extend(candidates)
                sequence=seq
        expiration=wire(expires_at)
        context=sync_seen_context(subject,sequence,expiration)
        token=app.cursors.encode('sync-v2',{'subject':subject},
            {'seq':sequence,'seen_ciphertext':seal_sync_seen(app,seen,context),
             'principal':principal,'watch_digest':watch_hash,
             'authorization_epoch':authorization_epoch,
             'topic_membership_digest':topic_membership_hash,
             'expires_at':expiration})
        require(len('/_r/s/'+token)<=min(8192,app.settings.server.limits.max_path_bytes),
                'resync_required')
        return HandlerOutput(data={'items':items,'sync_cursor':token,'next':'/_r/s/'+token,
                                   'next_requires_auth':True})
    from msg.plugins.collaboration import install as install_collaboration
    install_collaboration(app, op)
    from msg.plugins.collaboration_resources import install as install_records, resource_types
    install_records(app, op)
    from msg.plugins.following import install as install_following
    install_following(app, op)
    finish((ResourceTypeSpec(name='claim',version=1,container=False,content_schema=None,
                             operations=frozenset(),relations=frozenset()), *resource_types(app)))
