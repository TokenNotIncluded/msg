"""Topics, posts, files and templates all use Resource + immutable Revision."""
from __future__ import annotations
from dataclasses import replace
import time
from hashlib import sha256
from msg.constants import ROOT_SPACE,ROOT_SUBJECT
from msg.core.codec import canonical,decode,loads,wire,unb64,digest,parse_time
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef,Relation,EffectJob,BlobRef,Event,TemplateSpec
from msg.core.tags import normalize_tags
from msg.core.text_patch import (PATCH_LIMIT,PATCH_CONTEXT_LIMIT,PATCH_CANDIDATE_LIMIT,
    PATCH_SCHEMA,apply_text_patch,apply_patch,validate_patch)
from msg.core.template_dsl import parse_template,normalize_values
from msg.plugins.common import *
from msg.plugins.schemas import *
from msg.security.policy import STICKY


def topic_admin(tx,topic,subject):
    row=tx.one('SELECT role,status FROM topic_memberships WHERE topic=? AND subject=?',(topic,subject))
    return row==('admin','active')


def topic_member(tx,topic,subject):
    return tx.one('SELECT role,status,joined_at,invited_by FROM topic_memberships WHERE topic=? AND subject=?',
                  (topic,subject))


def active_topic_ban(tx,topic,subject,now):
    row=tx.one('SELECT expires_at FROM topic_bans WHERE topic=? AND subject=? AND status=?',
               (topic,subject,'active'))
    return row is not None and (row[0] is None or parse_time(row[0])>now)


TOPIC_EVENT_CODES={
    'topic.create':'tc','topic.member.join':'mj','topic.member.request':'mr',
    'topic.member.leave':'ml','topic.member.invite':'mi','topic.member.approve':'ma',
    'topic.member.remove':'mx','topic.member.promote':'mp','topic.member.demote':'md',
    'topic.member.ban':'mb','topic.member.unban':'mu','topic.policy.change':'pc',
    'topic.archive':'ta','topic.restore':'tr','topic.move':'tm',
    'topic.chmod':'tmo','topic.chgrp':'tgr','topic.chown':'tow','topic.configure':'tcf',
}


async def topic_governance_event(tx,ctx,request,topic,action,target, *, reason=None):
    event=Event(id=new_id('te'),type='topic.'+action,time=ctx.now,
                request_id=request.request_id,actor=ctx.principal.actor,subject=ctx.principal.subject,
                resources=(ResourceRef(id=topic),),
                data={'topic_id':topic,'action':action,'target_subject':target,
                      **({'reason':reason} if reason else {})})
    await tx.append_event(event)
    tx.execute('INSERT INTO topic_event_projection (seq,topic) '
               'SELECT seq,? FROM events WHERE id=?',(topic,event.id),write=True)
    if target is not None:
        notice={'id':new_id('message'),'sender':ctx.principal.subject,'actor':ctx.principal.actor,
                'recipient':target,'resource':{'id':target,'revision':None},'time':wire(ctx.now),
                'state':'delivered','source':'topic_governance','topic_id':topic,'action':action}
        tx.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                   (notice['id'],ctx.principal.subject,target,target,event.id,canonical(notice).decode()),write=True)


async def template_content(app,ctx,request,tx,parent,template,values):
    if isinstance(template,str):
        if '/' in template or template.startswith('tpl_'):
            rid=await resolve(tx,template)
        else:
            local=await tx.path(parent)
            try:
                rid=await tx.resolve(local+'/templates/'+template)
            except Failure as exc:
                if exc.code!='not_found':
                    raise
                rid=await tx.resolve('/templates/'+template)
        resource=await tx.resource(rid)
        ref=ResourceRef(id=rid,revision=resource.revision)
    else:
        rid=await resolve(tx,template['id'])
        version=template.get('version')
        revision=tx.setting(f'template_version:{rid}:{version}') if version is not None else (await tx.resource(rid)).revision
        require(revision is not None,'template_version_not_found')
        ref=ResourceRef(id=rid,revision=revision)
    await check_access(app,ctx,request,tx,rid,'read')
    resource=await tx.resource(rid)
    require(resource.type=='template','not_a_template')
    rev=await tx.revision(ref)
    source=(await app.contents.read_bytes(rev.content)).decode('utf-8')
    parsed=parse_template(source)
    # Installed defaults have a frozen field contract. User-owned templates and
    # later revisions remain resource data, parsed under the same finite DSL.
    try:
        registered=app.registry.template(rid,parsed.version)
    except Failure as exc:
        if exc.code!='unknown_template':
            raise
        registered=None
    fields=registered if registered is not None and registered.digest==rev.content.digest else parsed
    normalized=normalize_values(fields,values or {})
    content={'template_id':rid,'template_version':parsed.version,'template_digest':rev.content.digest,'values':normalized}
    return canonical(content),'application/json',content,Relation(type='template',target=ref)


async def create_post(app,ctx,request,tx, *, parent,relations=()):
    args=request.arguments
    policy=tx.setting('policy:'+parent,{})
    require(policy.get('editable',True),'content_frozen')
    metadata={}
    if args.get('template') is not None:
        require('body' not in args and 'source' not in args,'ambiguous_content')
        body,media,meta,relation=await template_content(app,ctx,request,tx,parent,args['template'],args.get('values',{}))
        metadata={k:v for k,v in meta.items() if k!='values'}
        relations=(*relations,relation)
    elif args.get('source') is not None:
        require('body' not in args,'ambiguous_content')
        body,media=await source_content(app,ctx,request,tx,args['source'])
    else:
        require('body' in args,'body_required')
        body,media=args['body'],'text/markdown'
    if not isinstance(body,BlobRef):
        require(len(body.encode() if isinstance(body,str) else body)<=app.settings.server.limits.max_request_bytes,'use_transfer')
    resource=await create_resource(app,ctx,request,tx,parent=parent,type='post',name=args.get('name'),
        body=body,media_type=media,relations=relations,resource_id=args.get('resource_id'),
        revision_id=args.get('revision_id'),content_signature=args.get('content_signature'))
    return resource,metadata


async def source_content(app,ctx,request,tx,value):
    import codecs
    from msg.plugins.communication import direct_ancestor
    ref=decode(ResourceRef,value)
    require(ref.revision is not None,'source_revision_required')
    await check_access(app,ctx,request,tx,ref.id,'read')
    require(await direct_ancestor(tx,ref.id) is None,'dm_reference_private')
    revision=await tx.revision(ref)
    require(revision.content.media_type in {'text/plain','text/markdown'},'text_source_required')
    # Validation does not normalize or rewrite signed source bytes.
    decoder=codecs.getincrementaldecoder('utf-8')()
    try:
        async for piece in app.contents.read(revision.content):
            decoder.decode(piece)
        decoder.decode(b'',final=True)
    except UnicodeError as exc:
        raise Failure('invalid_utf8') from exc
    return revision.content,revision.content.media_type


async def removable(app,ctx,request,tx,resource):
    chain=(*await tx.ancestors(resource.id),resource)
    require(not any(item.id=='t_last_will' for item in chain),'legacy_directive_only')
    require(not any(parent.type=='user' and child.name in {'SOUL.md','AGENTS.md','notes','todos'}
                    for parent,child in zip(chain,chain[1:])),
            'personal_managed_resource')
    require(resource.id!='r_agents' and all(
        ancestor.id!='r_rules' for ancestor in await tx.ancestors(resource.id)) and
        resource.id!='r_rules','system_managed_resource')
    require(resource.parent is not None and resource.id!=ROOT_SUBJECT,'protected_resource')
    parent=await tx.resource(resource.parent)
    await check_access(app,ctx,request,tx,parent.id,'remove')
    if parent.mode&STICKY:
        require(ctx.principal.subject in {resource.owner,parent.owner},'sticky_denied')
    return parent


async def require_unmanaged_personal(tx,resource):
    chain=(*await tx.ancestors(resource.id),resource)
    require(not any(parent.type=='user' and child.name in {'SOUL.md','AGENTS.md','notes','todos'}
                    for parent,child in zip(chain,chain[1:])),
            'personal_managed_resource')


async def ensure_public_repositories(tx,resource, *, mode=None,parent=None):
    repos=tx.rows("SELECT id FROM resources WHERE type='repo' AND state='active'")
    for (rid,) in repos:
        repo=await tx.resource(rid)
        ancestry=await tx.ancestors(rid)
        if rid==resource.id:
            require((repo.mode if mode is None else mode)&4,'repo_public_read_required')
        if any(a.id==resource.id for a in ancestry):
            if mode is not None:
                require(mode&1,'repo_public_read_required')
            if parent is not None:
                new_parent=await tx.resource(parent)
                require(new_parent.mode&1 and all(a.mode&1 for a in await tx.ancestors(parent)),
                        'repo_public_read_required')
    if resource.type=='repo' and parent is not None:
        p=await tx.resource(parent)
        require(p.mode&1 and all(a.mode&1 for a in await tx.ancestors(parent)),'repo_public_read_required')


async def editable_resource(app,ctx,request,tx,identifier):
    """One authority and lifecycle boundary for body replacement and patch."""
    resource=await tx.resource(await resolve(tx,identifier))
    require(resource.type in {'post','file'} and resource.state=='active','not_editable')
    chain=(*await tx.ancestors(resource.id),resource)
    require(not any(item.id=='t_last_will' for item in chain),'legacy_directive_only')
    require(resource.id!='r_agents' and all(item.id!='r_rules' for item in chain),
            'system_managed_resource')
    await require_unmanaged_personal(tx,resource)
    await check_access(app,ctx,request,tx,resource.id,'write')
    await assert_generation(request,resource)
    require((await topic_policy(tx,resource)).get('editable',True),'content_frozen')
    return resource


async def prepare_text_patch(app,ctx,request,tx,args, *, post_only=False):
    """Prepare an authorized edit without publishing any content or SQL reference."""
    resource=await editable_resource(app,ctx,request,tx,args['id'])
    if post_only:
        require(resource.type=='post','not_editable')
    current=await tx.revision(ResourceRef(id=resource.id,revision=resource.revision))
    media=current.content.media_type
    require(media in {'text/plain','text/markdown'},'text_patch_required')
    patch=args.get('patch')
    if patch is not None:
        validate_patch(patch)
    else:
        pieces=(args['exact'],args['replacement'],args.get('before',''),args.get('after',''))
        require(sum(len(piece.encode('utf-8')) for piece in pieces)<=PATCH_LIMIT,'patch_too_large')
    base_source=None
    require(current.content.size<=PATCH_LIMIT,'patch_too_large')
    try:
        source=(await app.contents.read_bytes(current.content,limit=PATCH_LIMIT)).decode('utf-8')
    except UnicodeError as exc:
        raise Failure('invalid_utf8') from exc
    stale=current.id!=args['base_revision']
    if stale:
        require(args.get('rebase') is True,'revision_conflict',details={'revision':current.id})
        require('base_generation' in args,'base_generation_required')
        require(args['base_generation']<resource.generation,'base_generation_conflict')
        base=await tx.revision(ResourceRef(id=resource.id,revision=args['base_revision']))
        # Never rebase across an unrelated historical branch.
        ancestor=current
        seen=set()
        while ancestor.id!=base.id:
            require(len(seen)<4096 and time.monotonic()<ctx.deadline_monotonic,'patch_too_complex')
            require(ancestor.id not in seen and len(ancestor.parents)==1,'revision_conflict')
            seen.add(ancestor.id)
            ancestor=await tx.revision(ResourceRef(id=resource.id,revision=ancestor.parents[0]))
        require(base.content.media_type==media and base.content.size<=PATCH_LIMIT,'text_patch_required')
        try:
            base_source=(await app.contents.read_bytes(base.content,limit=PATCH_LIMIT)).decode('utf-8')
        except UnicodeError as exc:
            raise Failure('invalid_utf8') from exc
        # A unique anchor must have selected exactly one block in the base.
        if patch is None:
            apply_text_patch(base_source,*pieces)
    else:
        require('base_generation' not in args or args['base_generation']==resource.generation,
                'base_generation_conflict')
    patched=(apply_patch(source,patch,base_source=base_source) if patch is not None
             else apply_text_patch(source,*pieces))
    return resource,current,patched,media


def install(app):
    from msg.bootstrap import manifest
    for name,source in manifest()['templates'].items():
        parsed=parse_template(source)
        require(name==parsed.name,'template_name_mismatch')
        app.registry.add_template(TemplateSpec(resource=ResourceRef(id='tpl_'+name),
            digest='sha256:'+sha256(source.encode()).hexdigest(),fields=parsed.fields,
            renderer_version=1),version=parsed.version)
    op,finish=registration(app,'content',('identity',))
    post_fields={'parent':IDENTIFIER,'name':STRING,'body':STRING,'template':{'anyOf':[STRING,obj({'id':IDENTIFIER,'version':INTEGER},('id',))]},
                 'values':{'type':'object'},'source':REF,'content_created_at':STRING,'resource_id':IDENTIFIER,'revision_id':IDENTIFIER,'content_signature':SIGNATURE}
    app.post_fields=post_fields

    @op('content.topic_create',obj({'parent':IDENTIFIER,'name':STRING},('parent','name')),
        requirements=requirement('parent','create'))
    async def topic_create(ctx,request,tx):
        resource=await create_resource(app,ctx,request,tx,parent=await resolve(tx,request.arguments['parent']),
            type='topic',name=request.arguments['name'])
        tx.execute('INSERT INTO topic_settings (topic,membership_policy) VALUES (?,?)',
                   (resource.id,'open'),write=True)
        tx.execute('''INSERT INTO topic_memberships
            (topic,subject,role,status,joined_at,invited_by) VALUES (?,?,?,?,?,?)''',
            (resource.id,ctx.principal.subject,'admin','active',wire(ctx.now),None),write=True)
        await topic_governance_event(tx,ctx,request,resource.id,'create',ctx.principal.subject)
        return output_for(resource)

    async def governed_topic(ctx,request,tx, *, admin=False):
        topic=await resolve(tx,request.arguments['id'])
        resource=await tx.resource(topic)
        require(resource.type=='topic' and resource.state=='active','topic_not_active')
        await require_unmanaged_personal(tx,resource)
        require(ctx.principal.subject is not None and ctx.principal.actor==ctx.principal.subject,
                'topic_subject_required')
        await app.authorizer.require_base(ctx.principal,operation_id(request),topic,tx)
        await check_access(app,ctx,request,tx,topic,'read')
        if admin:
            require(topic_admin(tx,topic,ctx.principal.subject),'topic_admin_required')
        return topic

    @op('content.topic_join',obj({'id':IDENTIFIER},('id',)),signature=True)
    async def topic_join(ctx,request,tx):
        topic=await governed_topic(ctx,request,tx)
        subject=ctx.principal.subject
        require(not active_topic_ban(tx,topic,subject,ctx.now),'topic_banned')
        previous=topic_member(tx,topic,subject)
        require(previous is None or previous[1] not in {'active','pending'},'already_topic_member')
        policy=tx.one('SELECT membership_policy FROM topic_settings WHERE topic=?',(topic,))
        membership_policy=policy[0] if policy else 'open'
        require(membership_policy in {'open','approval'} or
                (membership_policy=='invite' and previous is not None and previous[1]=='invited'),
                'topic_join_closed')
        status='pending' if membership_policy=='approval' and (previous is None or previous[1]!='invited') else 'active'
        if previous is None:
            tx.execute('INSERT INTO topic_memberships VALUES (?,?,?,?,?,?)',
                       (topic,subject,'member',status,wire(ctx.now) if status=='active' else None,None),write=True)
        else:
            tx.execute('''UPDATE topic_memberships SET role='member',status=?,joined_at=?
                WHERE topic=? AND subject=?''',(status,wire(ctx.now) if status=='active' else None,
                topic,subject),write=True)
        await topic_governance_event(tx,ctx,request,topic,'member.join' if status=='active' else 'member.request',subject)
        return HandlerOutput(resources=(ResourceRef(id=topic),),data={'topic_id':topic,'status':status,'role':'member'})

    @op('content.topic_leave',obj({'id':IDENTIFIER},('id',)),signature=True)
    async def topic_leave(ctx,request,tx):
        topic=await governed_topic(ctx,request,tx)
        subject=ctx.principal.subject
        previous=topic_member(tx,topic,subject)
        require(previous is not None and previous[1] in {'active','pending','invited'},'not_topic_member')
        if previous[0]=='admin' and previous[1]=='active':
            count=tx.one("SELECT COUNT(*) FROM topic_memberships WHERE topic=? AND role='admin' AND status='active'",(topic,))[0]
            require(count>1,'last_topic_admin')
        tx.execute("UPDATE topic_memberships SET role='member',status='left' WHERE topic=? AND subject=?",
                   (topic,subject),write=True)
        await topic_governance_event(tx,ctx,request,topic,'member.leave',subject)
        return HandlerOutput(resources=(ResourceRef(id=topic),),data={'topic_id':topic,'status':'left'})

    member_target=obj({'id':IDENTIFIER,'subject_id':IDENTIFIER},('id','subject_id'))

    @op('content.topic_invite',member_target,signature=True)
    async def topic_invite(ctx,request,tx):
        topic=await governed_topic(ctx,request,tx,admin=True)
        target=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(target)
        require(not active_topic_ban(tx,topic,target,ctx.now),'topic_banned')
        prior=topic_member(tx,topic,target)
        require(prior is None or prior[1] not in {'active','pending','invited'},'already_topic_member')
        if prior is None:
            tx.execute('INSERT INTO topic_memberships VALUES (?,?,?,?,?,?)',
                       (topic,target,'member','invited',None,ctx.principal.subject),write=True)
        else:
            tx.execute('''UPDATE topic_memberships SET role='member',status='invited',joined_at=NULL,
                invited_by=? WHERE topic=? AND subject=?''',(ctx.principal.subject,topic,target),write=True)
        await topic_governance_event(tx,ctx,request,topic,'member.invite',target)
        return HandlerOutput(resources=(ResourceRef(id=topic),),data={'topic_id':topic,'status':'invited'})

    @op('content.topic_approve',member_target,signature=True)
    async def topic_approve(ctx,request,tx):
        topic=await governed_topic(ctx,request,tx,admin=True)
        target=await resolve(tx,request.arguments['subject_id'])
        previous=topic_member(tx,topic,target)
        require(previous is not None and previous[1]=='pending','topic_request_not_pending')
        require(not active_topic_ban(tx,topic,target,ctx.now),'topic_banned')
        tx.execute("UPDATE topic_memberships SET status='active',joined_at=? WHERE topic=? AND subject=?",
                   (wire(ctx.now),topic,target),write=True)
        await topic_governance_event(tx,ctx,request,topic,'member.approve',target)
        return HandlerOutput(resources=(ResourceRef(id=topic),),data={'topic_id':topic,'status':'active'})

    async def update_membership(ctx,request,tx):
        topic=await governed_topic(ctx,request,tx,admin=True)
        target=await resolve(tx,request.arguments['subject_id'])
        previous=topic_member(tx,topic,target)
        require(previous is not None and previous[1]=='active','not_topic_member')
        action=request.operation.removeprefix('content.topic_')
        if action in {'remove','demote'} and previous[0]=='admin':
            count=tx.one("SELECT COUNT(*) FROM topic_memberships WHERE topic=? AND role='admin' AND status='active'",(topic,))[0]
            require(count>1,'last_topic_admin')
        if action=='remove':
            tx.execute("UPDATE topic_memberships SET role='member',status='removed' WHERE topic=? AND subject=?",
                       (topic,target),write=True)
            data={'status':'removed'}
        else:
            role='admin' if action=='promote' else 'member'
            require(previous[0]!=role,'topic_role_unchanged')
            tx.execute('UPDATE topic_memberships SET role=? WHERE topic=? AND subject=?',
                       (role,topic,target),write=True)
            data={'role':role}
        await topic_governance_event(tx,ctx,request,topic,'member.'+action,target)
        return HandlerOutput(resources=(ResourceRef(id=topic),),data={'topic_id':topic,**data})
    for name in ('content.topic_remove','content.topic_promote','content.topic_demote'):
        op(name,member_target,signature=True)(update_membership)

    @op('content.topic_ban',obj({'id':IDENTIFIER,'subject_id':IDENTIFIER,
        'reason':{'type':'string','maxLength':500},'expires_at':STRING},('id','subject_id')),signature=True)
    async def topic_ban(ctx,request,tx):
        topic=await governed_topic(ctx,request,tx,admin=True)
        target=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(target)
        previous=topic_member(tx,topic,target)
        if previous is not None and previous[:2]==('admin','active'):
            count=tx.one("SELECT COUNT(*) FROM topic_memberships WHERE topic=? AND role='admin' AND status='active'",(topic,))[0]
            require(count>1,'last_topic_admin')
        expires=request.arguments.get('expires_at')
        if expires:
            require(parse_time(expires)>ctx.now,'topic_ban_expired')
        tx.execute('''INSERT INTO topic_bans (topic,subject,actor,created_at,expires_at,reason,status)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(topic,subject) DO UPDATE SET
            actor=excluded.actor,created_at=excluded.created_at,expires_at=excluded.expires_at,
            reason=excluded.reason,status='active' ''',
            (topic,target,ctx.principal.subject,wire(ctx.now),expires,request.arguments.get('reason'),'active'),write=True)
        if previous is not None:
            tx.execute("UPDATE topic_memberships SET role='member',status='removed' WHERE topic=? AND subject=?",
                       (topic,target),write=True)
        await topic_governance_event(tx,ctx,request,topic,'member.ban',target,
                                     reason=request.arguments.get('reason'))
        return HandlerOutput(resources=(ResourceRef(id=topic),),data={'topic_id':topic,'banned_subject':target})

    @op('content.topic_unban',member_target,signature=True)
    async def topic_unban(ctx,request,tx):
        topic=await governed_topic(ctx,request,tx,admin=True)
        target=await resolve(tx,request.arguments['subject_id'])
        changed=tx.execute("UPDATE topic_bans SET status='lifted' WHERE topic=? AND subject=? AND status='active'",
                           (topic,target),write=True).rowcount
        require(changed==1,'topic_ban_not_found')
        await topic_governance_event(tx,ctx,request,topic,'member.unban',target)
        return HandlerOutput(resources=(ResourceRef(id=topic),),data={'topic_id':topic,'unbanned_subject':target,
            'membership_restored':False})

    @op('content.topic_policy_set',obj({'id':IDENTIFIER,
        'membership_policy':{'enum':['open','approval','invite','closed']}},
        ('id','membership_policy')),signature=True)
    async def topic_policy_set(ctx,request,tx):
        topic=await governed_topic(ctx,request,tx,admin=True)
        policy=request.arguments['membership_policy']
        tx.execute('''INSERT INTO topic_settings (topic,membership_policy) VALUES (?,?)
            ON CONFLICT(topic) DO UPDATE SET membership_policy=excluded.membership_policy''',
            (topic,policy),write=True)
        await topic_governance_event(tx,ctx,request,topic,'policy.change',ctx.principal.subject)
        return HandlerOutput(resources=(ResourceRef(id=topic),),data={'topic_id':topic,
            'membership_policy':policy})

    @op('content.topic_members',obj({'id':IDENTIFIER},('id',)),effect='read')
    async def topic_members(ctx,request,tx):
        topic=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,topic,'read')
        require((await tx.resource(topic)).type=='topic','not_a_topic')
        rows=tx.rows("SELECT subject,role,status,joined_at FROM topic_memberships WHERE topic=? AND status='active' ORDER BY subject",
                     (topic,))
        policy=tx.one('SELECT membership_policy FROM topic_settings WHERE topic=?',(topic,))
        return HandlerOutput(data={'topic_id':topic,'membership_policy':policy[0] if policy else 'open',
            'items':[{'subject_id':subject,'role':role,'status':status,'joined_at':joined}
                     for subject,role,status,joined in rows]})

    @op('content.topic_events',obj({'id':IDENTIFIER,
        'view':{'enum':['compact','normal','proof']},'cursor':STRING,
        'limit':{'type':'integer','minimum':1,'maximum':50}},('id',)),effect='read')
    async def topic_events(ctx,request,tx):
        from msg.plugins.discovery import next_link
        topic=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,topic,'read')
        require((await tx.resource(topic)).type=='topic','not_a_topic')
        view=request.arguments.get('view','compact')
        admin=topic_admin(tx,topic,ctx.principal.subject)
        binding=digest({'topic':topic,'view':view,'subject':ctx.principal.subject,'admin':admin})
        position=app.cursors.decode(request.arguments['cursor'],'topic_events',binding) if request.arguments.get('cursor') else 9223372036854775807
        limit=request.arguments.get('limit',10)
        found=[]
        # Limit the indexed projection BEFORE joining immutable event bodies.
        rows=tx.execute('SELECT p.seq,e.body FROM '
            '(SELECT seq FROM topic_event_projection WHERE topic=? AND seq<? '
            'ORDER BY seq DESC LIMIT ?) p JOIN events e ON e.seq=p.seq ORDER BY p.seq DESC',
            (topic,position,limit+1))
        for seq,raw in rows:
            found.append((seq,loads(raw)))
        items=[]
        for seq,event in found[:limit]:
            data=dict(event['data'])
            if not admin:
                data.pop('reason',None)
            if view=='compact':
                items.append({'s':seq,'c':TOPIC_EVENT_CODES[event['type']],
                              'a':event['actor'],'u':data.get('target_subject'),'t':event['time']})
            elif view=='normal':
                line=f"{event['time']} {event['actor']} {data['action']} {data.get('target_subject') or ''}"
                if admin and data.get('reason'):
                    line+=' — '+data['reason']
                items.append({'seq':seq,'text':line})
            else:
                proof=dict(event,data=data)
                result=tx.one('SELECT body FROM results WHERE subject=? AND request_id=?',
                              (event['subject'],event['request_id']))
                if result is not None:
                    proof['receipt']=loads(result[0]).get('receipt')
                items.append({'seq':seq,'event':proof})
        response={'topic_id':topic,'view':view,'items':items}
        if view=='compact':
            response.update(schema_version=1,event_codes=TOPIC_EVENT_CODES,
                            base_time=found[0][1]['time'] if found else None)
        if len(found)>limit:
            cursor=app.cursors.encode('topic_events',binding,found[limit-1][0])
            response.update(cursor=cursor,
                next=next_link(app,'content.topic_events',{**request.arguments,'cursor':cursor}),
                next_requires_auth=ctx.principal.subject is not None)
        return HandlerOutput(data=response)

    @op('content.post_create',obj(post_fields,('parent',)),requirements=requirement('parent','create'))
    async def post_create(ctx,request,tx):
        resource,meta=await create_post(app,ctx,request,tx,parent=await resolve(tx,request.arguments['parent']))
        return output_for(resource,**meta)

    @op('content.tags_set',obj({'id':IDENTIFIER,'tags':{'type':'array','items':STRING,
        'maxItems':16}},('id','tags')))
    async def tags_set(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await require_unmanaged_personal(tx,resource)
        check='manage' if resource.type in {'topic','repo'} else 'write'
        await check_access(app,ctx,request,tx,resource.id,check)
        require(app.registry.resource_type(resource.type,resource.type_version).taggable,
                'resource_not_taggable')
        require(resource.state=='active','resource_inactive')
        await assert_generation(request,resource)
        tags=normalize_tags(request.arguments['tags'])
        if tags==resource.tags:
            return output_for(resource,tags=tags)
        updated=replace(resource,tags=tags,generation=resource.generation+1,
                        modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated,tags=tags)

    post_write_schema=obj({'id':IDENTIFIER,'expected_revision':IDENTIFIER,'body':STRING,
        'template':post_fields['template'],'values':{'type':'object'},'source':REF,'content_created_at':STRING,'revision_id':IDENTIFIER,'content_signature':SIGNATURE},
        ('id','expected_revision'))
    @op('content.post_write',post_write_schema,requirements=requirement('id','write'))
    @op('content.post_edit',post_write_schema,requirements=requirement('id','write'))
    async def post_edit(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        require(resource.type=='post' and resource.state=='active','not_editable')
        await assert_generation(request,resource)
        require(resource.revision==request.arguments['expected_revision'],'revision_conflict',details={'revision':resource.revision})
        require((await topic_policy(tx,resource)).get('editable',True),'content_frozen')
        old=await tx.revision(ResourceRef(id=resource.id))
        relations=tuple(r for r in old.relations if r.type!='template')
        if request.arguments.get('template') is not None:
            require('body' not in request.arguments and 'source' not in request.arguments,'ambiguous_content')
            body,media,meta,relation=await template_content(app,ctx,request,tx,resource.parent,
                request.arguments['template'],request.arguments.get('values',{}))
            relations=(*relations,relation)
        elif request.arguments.get('source') is not None:
            require('body' not in request.arguments,'ambiguous_content')
            body,media=await source_content(app,ctx,request,tx,request.arguments['source'])
        else:
            require('body' in request.arguments,'body_required')
            body,media=request.arguments['body'],'text/markdown'
        updated=await revise_resource(app,ctx,request,tx,resource,body,media,relations=relations,author=old.author,
            signature=request.arguments.get('content_signature'),revision_id=request.arguments.get('revision_id'))
        return output_for(updated)

    @op('content.post_edit_metadata',obj({'id':IDENTIFIER,'name':STRING,
        'tags':{'type':'array','items':STRING,'maxItems':16}},('id',)),
        requirements=requirement('id','write'))
    async def post_edit_metadata(ctx,request,tx):
        a=request.arguments
        require('name' in a or 'tags' in a,'metadata_change_required')
        resource=await tx.resource(await resolve(tx,a['id']))
        require(resource.type=='post' and resource.state=='active','not_editable')
        await require_unmanaged_personal(tx,resource)
        await assert_generation(request,resource)
        require((await topic_policy(tx,resource)).get('editable',True),'content_frozen')
        name=resource.name
        if 'name' in a:
            # Names are path metadata. Keep the canonical .md post path and the
            # same protected-namespace/sticky checks as an explicit move.
            await removable(app,ctx,request,tx,resource)
            name=validate_name(a['name'] if a['name'].endswith('.md') else a['name']+'.md')
            await protect_namespace(app,ctx,request,tx,resource.parent,name)
        tags=normalize_tags(a['tags']) if 'tags' in a else resource.tags
        if name==resource.name and tags==resource.tags:
            return output_for(resource,name=name,tags=tags)
        updated=replace(resource,name=name,tags=tags,generation=resource.generation+1,
                        modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated,name=name,tags=tags)

    @op('content.post_rollback',obj({'id':IDENTIFIER,'base_revision':IDENTIFIER,
        'target_revision':IDENTIFIER,'content_created_at':STRING,
        'revision_id':IDENTIFIER,'content_signature':SIGNATURE},
        ('id','base_revision','target_revision')),requirements=requirement('id','write'))
    async def post_rollback(ctx,request,tx):
        a=request.arguments
        resource=await tx.resource(await resolve(tx,a['id']))
        require(resource.type=='post' and resource.state=='active','not_editable')
        await require_unmanaged_personal(tx,resource)
        await assert_generation(request,resource)
        require(resource.revision==a['base_revision'],'revision_conflict',
                details={'revision':resource.revision})
        require(a['target_revision']!=resource.revision,'rollback_target_current')
        require((await topic_policy(tx,resource)).get('editable',True),'content_frozen')
        current=await tx.revision(ResourceRef(id=resource.id,revision=resource.revision))
        historical=await tx.revision(ResourceRef(id=resource.id,revision=a['target_revision']))
        require(historical.author==current.author,'rollback_author_mismatch')
        manifest={k:v for k,v in wire(historical).items() if k not in {'manifest_digest','signature'}}
        require(digest(manifest)==historical.manifest_digest,'revision_manifest_mismatch')
        if historical.signature is not None:
            credential=await tx.credential(historical.signature.key_id)
            require(credential.subject_id==historical.subject and credential.kind=='signing_key',
                    'revision_signer_mismatch')
            verify(credential.verifier,canonical(manifest),historical.signature,purpose='revision')
        hasher=sha256()
        size=0
        async for chunk in app.contents.read(historical.content):
            size+=len(chunk)
            hasher.update(chunk)
        require(size==historical.content.size and
                'sha256:'+hasher.hexdigest()==historical.content.digest,
                'content_digest_mismatch')
        # Revalidate every historical relation under present-day authorization.
        # revise_resource creates a new manifest and signature; it never edits
        # or adopts the historical signature as proof of the new revision.
        updated=await revise_resource(app,ctx,request,tx,resource,historical.content,
            historical.content.media_type,relations=historical.relations,author=current.author,
            signature=a.get('content_signature'),revision_id=a.get('revision_id'))
        return output_for(updated,rolled_back_from=historical.id)

    patch_fields={'id':IDENTIFIER,'base_revision':IDENTIFIER,
        'exact':{'type':'string','minLength':1,'maxLength':PATCH_LIMIT},
        'replacement':{'type':'string','maxLength':PATCH_LIMIT},
        'before':{'type':'string','maxLength':PATCH_CONTEXT_LIMIT},
        'after':{'type':'string','maxLength':PATCH_CONTEXT_LIMIT}}
    post_patch_schema=obj(patch_fields,('id','base_revision','exact','replacement'))
    rebase_patch_fields={**patch_fields,'base_generation':INTEGER,'rebase':BOOLEAN}
    rebase_patch_schema=obj(rebase_patch_fields,('id','base_revision','base_generation','exact','replacement'))
    structured_patch_schema=obj({'id':IDENTIFIER,'base_revision':IDENTIFIER,
        'base_generation':INTEGER,'rebase':BOOLEAN,'patch':PATCH_SCHEMA,
        'change_note':{'type':'string','maxLength':2048},
        'content_created_at':STRING,'revision_id':IDENTIFIER,'content_signature':SIGNATURE},
        ('id','base_revision','base_generation','patch'))

    async def publish_patch(ctx,request,tx,args,prepared):
        resource,old,body,media=prepared
        extra={}
        if 'patch' in args:
            # Bind provenance to the precise patch, not to the signed request
            # containing this signature (which would create a digest cycle).
            extra={'change_note':args.get('change_note'),'source_kind':'user',
                   'source_version':1,'source_digest':digest(args['patch']),
                   'signature':args.get('content_signature'),'revision_id':args.get('revision_id'),
                   'content_created_at':args.get('content_created_at')}
        return await revise_resource(app,ctx,request,tx,resource,body,media,
            relations=old.relations,author=old.author,**extra)

    @op('content.post_patch',post_patch_schema,requirements=requirement('id','write'))
    @op('content.post_patch',structured_patch_schema,requirements=requirement('id','write'),version=2)
    @op('content.text_patch',post_patch_schema,requirements=requirement('id','write'))
    @op('content.text_patch',rebase_patch_schema,requirements=requirement('id','write'),version=2)
    @op('content.text_patch',structured_patch_schema,requirements=requirement('id','write'),version=3)
    async def text_patch(ctx,request,tx):
        args=request.arguments
        prepared=await prepare_text_patch(app,ctx,request,tx,args,
            post_only=request.operation=='content.post_patch')
        updated=await publish_patch(ctx,request,tx,args,prepared)
        return output_for(updated)

    @op('content.text_patch_batch',obj({'patches':{'type':'array','minItems':1,'maxItems':16,
        'items':rebase_patch_schema}},('patches',)))
    @op('content.text_patch_batch',obj({'patches':{'type':'array','minItems':1,'maxItems':16,
        'items':structured_patch_schema}},('patches',)),version=2)
    async def text_patch_batch(ctx,request,tx):
        patches=request.arguments['patches']
        resolved=[await resolve(tx,item['id']) for item in patches]
        require(len(set(resolved))==len(resolved),'duplicate_patch_target')
        # Validate every target before the first content put. The executor owns
        # the surrounding SQL transaction; a later signature/storage failure
        # rolls back all published references, leaving only reclaimable blobs.
        prepared=[]
        for item,rid in zip(patches,resolved):
            prepared.append(await prepare_text_patch(app,ctx,request,tx,{**item,'id':rid}))
        updated=[]
        for args,item in zip(patches,prepared):
            updated.append(await publish_patch(ctx,request,tx,args,item))
        data={'generations':{r.id:r.generation for r in updated}}
        if request.contract_version>=2:
            data['items']=[{'id':r.id,'revision':r.revision,'generation':r.generation} for r in updated]
        return HandlerOutput(resources=tuple(ResourceRef(id=r.id,revision=r.revision) for r in updated),
            data=data)

    async def lifecycle(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        require(resource.type in {'post','file','attachment','topic','template','skill','repo','website','keystore'},'controlled_resource')
        await assert_generation(request,resource)
        await removable(app,ctx,request,tx,resource)
        require(resource.state!='purged','resource_purged')
        state='archived' if request.operation in {'content.archive','file.delete'} else 'active'
        if state=='active' and resource.type=='website':
            from msg.plugins.hosting_capacity import manifest_size,require_capacity
            await require_capacity(app,tx,resource,await manifest_size(app,tx,resource),ctx.now)
        if state=='active' and resource.type=='repo':
            await ensure_public_repositories(tx,resource,mode=resource.mode,parent=resource.parent)
        updated=replace(resource,state=state,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        if resource.type=='topic':
            await topic_governance_event(tx,ctx,request,resource.id,
                                         'archive' if state=='archived' else 'restore',ctx.principal.subject)
        return output_for(updated,state=state)
    for name in ('content.archive','content.restore'):
        op(name,obj({'id':IDENTIFIER},('id',)))(lifecycle)

    @op('content.move',obj({'id':IDENTIFIER,'parent':IDENTIFIER,'name':STRING},('id','parent')))
    async def move(ctx,request,tx):
        from msg.plugins.communication import direct_ancestor
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        require(resource.type in {'post','file','attachment','topic','template','skill','repo','website','keystore'},'controlled_resource')
        await assert_generation(request,resource)
        await removable(app,ctx,request,tx,resource)
        require(await direct_ancestor(tx,resource.id) is None,'dm_controlled_resource')
        target=await resolve(tx,request.arguments['parent'])
        require(target!='t_store','store_controlled_resource')
        await check_access(app,ctx,request,tx,target,'create')
        parent=await tx.resource(target)
        require(app.registry.resource_type(parent.type,1).container,'not_a_container')
        await ensure_public_repositories(tx,resource,parent=target)
        name=validate_name(request.arguments.get('name',resource.name))
        if parent.type=='user' and name in {'SOUL.md','AGENTS.md','notes'}:
            require(False,'personal_managed_resource')
        if (parent.name=='notes' and parent.parent is not None and
                (await tx.resource(parent.parent)).type=='user'):
            require(False,'personal_managed_resource')
        await protect_namespace(app,ctx,request,tx,parent,name)
        updated=replace(resource,parent=target,name=name,generation=resource.generation+1,
                        modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        if resource.type=='topic':
            await topic_governance_event(tx,ctx,request,resource.id,'move',ctx.principal.subject)
        return output_for(updated)

    @op('content.chmod',obj({'id':IDENTIFIER,'mode':{'type':'string','pattern':'^[0-7]{4}$'}},('id','mode')),
        requirements=requirement('id','chmod'),signature=True)
    async def chmod(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await require_unmanaged_personal(tx,resource)
        await assert_generation(request,resource)
        require(resource.type not in {'csr','certificate','tool','delegation','listing'} and
                resource.id!='t_store','controlled_resource')
        mode=int(request.arguments['mode'],8)
        await ensure_public_repositories(tx,resource,mode=mode)
        updated=replace(resource,mode=mode,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        if resource.type=='topic':
            await topic_governance_event(tx,ctx,request,resource.id,'chmod',ctx.principal.subject)
        return output_for(updated,mode=f'{mode:04o}')

    @op('content.chgrp',obj({'id':IDENTIFIER,'group':IDENTIFIER},('id','group')),
        requirements=requirement('id','chgrp'),signature=True)
    async def chgrp(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await require_unmanaged_personal(tx,resource)
        await assert_generation(request,resource)
        require(resource.type!='listing' and resource.id!='t_store','controlled_resource')
        group=await resolve(tx,request.arguments['group'])
        await tx.organization(group)
        member=any(m.organization_id==group for m in await tx.memberships(ctx.principal.subject))
        require(member or await app.authorizer.has(ctx.principal,'resource.chgrp_override',operation_id(request),resource.id,tx),
                'group_membership_required')
        updated=replace(resource,group=group,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        if resource.type=='topic':
            await topic_governance_event(tx,ctx,request,resource.id,'chgrp',ctx.principal.subject)
        return output_for(updated,group=group)

    @op('content.chown',obj({'id':IDENTIFIER,'owner':IDENTIFIER},('id','owner')),
        requirements=requirement('id','chown'),signature=True)
    async def chown(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await require_unmanaged_personal(tx,resource)
        await assert_generation(request,resource)
        require(resource.type not in {'user','organization','certificate','csr','tool','delegation','listing'} and
                resource.id!='t_store','controlled_resource')
        owner=await resolve(tx,request.arguments['owner'])
        subject=await tx.subject(owner)
        require(not subject.local_only,'local_only')
        if resource.type=='website' and resource.state=='active':
            from msg.plugins.hosting_capacity import manifest_size,require_capacity
            await require_capacity(app,tx,resource,await manifest_size(app,tx,resource),ctx.now,owner=owner)
        updated=replace(resource,owner=owner,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        if resource.type=='topic':
            await topic_governance_event(tx,ctx,request,resource.id,'chown',ctx.principal.subject)
        return output_for(updated,owner=owner)

    @op('content.purge',obj({'id':IDENTIFIER,'reason':STRING},('id','reason')),
        requirements=requirement('id','purge'),signature=True)
    async def purge(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await require_unmanaged_personal(tx,resource)
        await assert_generation(request,resource)
        require(resource.type in {'post','file','attachment','template','skill','keystore'},'purge_leaf_only')
        revisions=[decode(__import__('msg.core.models',fromlist=['Revision']).Revision,loads(r[0]))
                   for r in tx.rows('SELECT body FROM revisions WHERE resource_id=?',(resource.id,))]
        updated=replace(resource,state='purged',revision=None,generation=resource.generation+1,
                        modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        tx.execute('DELETE FROM revisions WHERE resource_id=?',(resource.id,),write=True)
        tx.execute('DELETE FROM projections WHERE resource_id=?',(resource.id,),write=True)
        from msg.plugins.communication import event_id
        job=EffectJob(id=new_id('job'),event_id=event_id(request,ctx.principal.subject),kind='gc.resource',dedupe_key='purge:'+ctx.principal.subject+':'+request.request_id,
            principal=ctx.principal,operation=request.operation,arguments={'contract_version':request.contract_version,'id':resource.id,'revisions':wire(revisions)},
            state='pending',attempts=0,next_attempt_at=ctx.now,lease_until=None)
        await tx.enqueue(job)
        return output_for(updated,state='purged',physical_cleanup=job.id,backup_scope='backups_require_separate_retention')

    @op('content.topic_configure',obj({'id':IDENTIFIER,'policy':obj({
        'reply_open':BOOLEAN,'editable':BOOLEAN,'post_mode':{'type':'string','pattern':'^[0-7]{4}$'},
        'file_mode':{'type':'string','pattern':'^[0-7]{4}$'},'topic_mode':{'type':'string','pattern':'^[0-7]{4}$'},
        'recommended_template':STRING,'retention_seconds':{'type':'integer','minimum':60},
        'cleanup_interval_seconds':{'type':'integer','minimum':60}})},('id','policy')),
        requirements=requirement('id','manage'),signature=True)
    async def configure(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await require_unmanaged_personal(tx,resource)
        require(resource.type=='topic','not_a_topic')
        await assert_generation(request,resource)
        tx.set_setting('policy:'+resource.id,request.arguments['policy'])
        updated=replace(resource,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        await topic_governance_event(tx,ctx,request,resource.id,'configure',ctx.principal.subject)
        return output_for(updated)

    @op('content.template_put',obj({'parent':IDENTIFIER,'source':STRING},('parent','source')),
        requirements=requirement('parent','create'),signature=True)
    async def template_put(ctx,request,tx):
        parent=await resolve(tx,request.arguments['parent'])
        source=request.arguments['source']
        template=parse_template(source)
        existing=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',(parent,template.name))
        if existing:
            resource=await tx.resource(existing[0])
            require(resource.type=='template','name_conflict')
            await check_access(app,ctx,request,tx,resource.id,'write')
            await assert_generation(request,resource)
            require(tx.setting(f'template_version:{resource.id}:{template.version}') is None,'template_version_exists')
            resource=await revise_resource(app,ctx,request,tx,resource,source,'application/msg-template')
        else:
            resource=await create_resource(app,ctx,request,tx,parent=parent,type='template',name=template.name,
                body=source,media_type='application/msg-template')
        tx.set_setting(f'template_version:{resource.id}:{template.version}',resource.revision)
        rev=await tx.revision(ResourceRef(id=resource.id))
        return output_for(resource,template_id=resource.id,template_version=template.version,template_digest=rev.content.digest)

    @op('content.file_put',obj({'parent':IDENTIFIER,'name':STRING,'data':BYTES,'source':REF,
        'media_type':STRING},('parent','name')),requirements=requirement('parent','create'))
    async def file_put(ctx,request,tx):
        a=request.arguments
        require(('data' in a)!=('source' in a),'one_content_source_required')
        media=a.get('media_type','application/octet-stream')
        require('\r' not in media and '\n' not in media,'invalid_media_type')
        if 'source' in a:
            ref=decode(ResourceRef,a['source'])
            await check_access(app,ctx,request,tx,ref.id,'read')
            body=(await tx.revision(ref)).content
            media=body.media_type
        else:
            body=unb64(a['data'],limit=app.settings.server.limits.max_request_bytes)
        resource=await create_resource(app,ctx,request,tx,parent=await resolve(tx,a['parent']),type='file',
            name=a['name'],body=body,media_type=media)
        return output_for(resource)

    @op('content.attach',obj({'post':IDENTIFIER,'source':REF,'name':STRING},('post','source')))
    async def attach(ctx,request,tx):
        post=await tx.resource(await resolve(tx,request.arguments['post']))
        require(post.type=='post' and post.state=='active','not_a_post')
        await assert_generation(request,post)
        await check_access(app,ctx,request,tx,post.id,'write')
        ref=decode(ResourceRef,request.arguments['source'])
        await check_access(app,ctx,request,tx,ref.id,'read')
        source=await tx.revision(ref)
        await check_access(app,ctx,request,tx,post.parent,'create')
        attached=await create_resource(app,ctx,request,tx,parent=post.parent,type='attachment',
            name=request.arguments.get('name',new_id('attachment')),body=source.content,media_type=source.content.media_type,
            mode=post.mode&0o777)
        tx.set_setting('publication:'+attached.id,{'source':wire(ref),'post':post.id,'publisher':ctx.principal.subject})
        old=await tx.revision(ResourceRef(id=post.id))
        updated=await revise_resource(app,ctx,request,tx,post,old.content,old.content.media_type,
            relations=(*old.relations,Relation(type='attachment',target=ResourceRef(id=attached.id,revision=attached.revision))),
            author=old.author)
        return output_for(updated,attachment=wire(ResourceRef(id=attached.id,revision=attached.revision)))
    finish()
