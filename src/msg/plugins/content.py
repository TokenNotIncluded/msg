"""Topics, posts, files and templates all use Resource + immutable Revision."""
from __future__ import annotations
from dataclasses import replace
from msg.constants import ROOT_SPACE,ROOT_SUBJECT
from msg.core.codec import canonical,decode,loads,wire,unb64,digest
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef,Relation,EffectJob,BlobRef
from msg.core.tags import normalize_tags
from msg.core.template_dsl import parse_template,normalize_values
from msg.plugins.common import *
from msg.plugins.schemas import *
from msg.security.policy import STICKY


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
    normalized=normalize_values(parsed,values or {})
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
    require(resource.parent is not None and resource.id!=ROOT_SUBJECT,'protected_resource')
    parent=await tx.resource(resource.parent)
    await check_access(app,ctx,request,tx,parent.id,'remove')
    if parent.mode&STICKY:
        require(ctx.principal.subject in {resource.owner,parent.owner},'sticky_denied')
    return parent


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


def install(app):
    op,finish=registration(app,'content',('identity',))
    post_fields={'parent':IDENTIFIER,'name':STRING,'body':STRING,'template':{'anyOf':[STRING,obj({'id':IDENTIFIER,'version':INTEGER},('id',))]},
                 'values':{'type':'object'},'source':REF,'content_created_at':STRING,'resource_id':IDENTIFIER,'revision_id':IDENTIFIER,'content_signature':SIGNATURE}
    app.post_fields=post_fields

    @op('content.topic_create',obj({'parent':IDENTIFIER,'name':STRING},('parent','name')),
        requirements=requirement('parent','create'))
    async def topic_create(ctx,request,tx):
        resource=await create_resource(app,ctx,request,tx,parent=await resolve(tx,request.arguments['parent']),
            type='topic',name=request.arguments['name'])
        return output_for(resource)

    @op('content.post_create',obj(post_fields,('parent',)),requirements=requirement('parent','create'))
    async def post_create(ctx,request,tx):
        resource,meta=await create_post(app,ctx,request,tx,parent=await resolve(tx,request.arguments['parent']))
        return output_for(resource,**meta)

    @op('content.tags_set',obj({'id':IDENTIFIER,'tags':{'type':'array','items':STRING,
        'maxItems':16}},('id','tags')))
    async def tags_set(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
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

    @op('content.post_edit',obj({'id':IDENTIFIER,'expected_revision':IDENTIFIER,'body':STRING,
        'template':post_fields['template'],'values':{'type':'object'},'source':REF,'content_created_at':STRING,'revision_id':IDENTIFIER,'content_signature':SIGNATURE},
        ('id','expected_revision')),requirements=requirement('id','write'))
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

    async def lifecycle(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        require(resource.type in {'post','file','attachment','topic','template','skill','repo','website','keystore'},'controlled_resource')
        await assert_generation(request,resource)
        await removable(app,ctx,request,tx,resource)
        require(resource.state!='purged','resource_purged')
        state='archived' if request.operation=='content.archive' else 'active'
        if state=='active' and resource.type=='repo':
            await ensure_public_repositories(tx,resource,mode=resource.mode,parent=resource.parent)
        updated=replace(resource,state=state,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
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
        await check_access(app,ctx,request,tx,target,'create')
        parent=await tx.resource(target)
        require(app.registry.resource_type(parent.type,1).container,'not_a_container')
        await ensure_public_repositories(tx,resource,parent=target)
        name=validate_name(request.arguments.get('name',resource.name))
        await protect_namespace(app,ctx,request,tx,parent,name)
        updated=replace(resource,parent=target,name=name,generation=resource.generation+1,
                        modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated)

    @op('content.chmod',obj({'id':IDENTIFIER,'mode':{'type':'string','pattern':'^[0-7]{4}$'}},('id','mode')),
        requirements=requirement('id','chmod'),signature=True)
    async def chmod(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await assert_generation(request,resource)
        require(resource.type not in {'csr','certificate','tool','delegation'},'controlled_resource')
        mode=int(request.arguments['mode'],8)
        await ensure_public_repositories(tx,resource,mode=mode)
        updated=replace(resource,mode=mode,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated,mode=f'{mode:04o}')

    @op('content.chgrp',obj({'id':IDENTIFIER,'group':IDENTIFIER},('id','group')),
        requirements=requirement('id','chgrp'),signature=True)
    async def chgrp(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await assert_generation(request,resource)
        group=await resolve(tx,request.arguments['group'])
        await tx.organization(group)
        member=any(m.organization_id==group for m in await tx.memberships(ctx.principal.subject))
        require(member or await app.authorizer.has(ctx.principal,'resource.chgrp_override',operation_id(request),resource.id,tx),
                'group_membership_required')
        updated=replace(resource,group=group,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated,group=group)

    @op('content.chown',obj({'id':IDENTIFIER,'owner':IDENTIFIER},('id','owner')),
        requirements=requirement('id','chown'),signature=True)
    async def chown(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
        await assert_generation(request,resource)
        require(resource.type not in {'user','organization','certificate','csr','tool','delegation'},'controlled_resource')
        owner=await resolve(tx,request.arguments['owner'])
        subject=await tx.subject(owner)
        require(not subject.local_only,'local_only')
        updated=replace(resource,owner=owner,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated,owner=owner)

    @op('content.purge',obj({'id':IDENTIFIER,'reason':STRING},('id','reason')),
        requirements=requirement('id','purge'),signature=True)
    async def purge(ctx,request,tx):
        resource=await tx.resource(await resolve(tx,request.arguments['id']))
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
            principal=ctx.principal,operation=request.operation,arguments={'id':resource.id,'revisions':wire(revisions)},
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
        require(resource.type=='topic','not_a_topic')
        await assert_generation(request,resource)
        tx.set_setting('policy:'+resource.id,request.arguments['policy'])
        updated=replace(resource,generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
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
