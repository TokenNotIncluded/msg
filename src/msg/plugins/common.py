"""Explicit resource use cases shared by content and extensions."""
from __future__ import annotations
import re
from dataclasses import replace
from uuid import uuid4
from msg.constants import *
from msg.core.codec import canonical,wire,digest,decode,loads,parse_time
from msg.core.errors import Failure,require
from msg.core.models import Resource,ResourceRef,Revision,AccessRequirement,HandlerOutput,Signature
from msg.security.crypto import verify
from msg.security.policy import inherit_group,SETGID


def new_id(prefix='r'):
    return prefix+'_'+uuid4().hex


def operation_id(request):
    return f'{request.operation}@{request.contract_version}'


async def resolve(session,value):
    if isinstance(value,str) and value.startswith('/'):
        return await session.resolve(value)
    require(isinstance(value,str) and bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',value)),'invalid_resource_id')
    return (await session.resource(value)).id


async def check_access(app,context,request,session,rid,check):
    return await app.authorizer.require(context,request,(AccessRequirement(resource_id=rid,
        operation=operation_id(request),check=check),),session)


def requirement(argument,check, *, default=None):
    async def requirements(request,session):
        target=request.arguments.get(argument,default)
        rid=await resolve(session,target)
        return (AccessRequirement(resource_id=rid,operation=operation_id(request),check=check),)
    return requirements


async def no_requirements(request,session):
    return ()


def validate_name(name, *, identity=False):
    require(isinstance(name,str) and 1<=len(name)<=120 and '/' not in name and '\\' not in name and
            name not in {'.','..','json','meta','raw','history','revisions'} and
            all(ord(c)>=32 and ord(c)!=127 for c in name),'invalid_name')
    if not identity:
        require(not name.startswith(('@','&','!','~')),'reserved_name')
    return name


async def protect_namespace(app,ctx,request,tx,parent,name):
    if name.startswith('_'):
        require(await app.authorizer.has(ctx.principal,'system.namespace',operation_id(request),parent.id,tx),
                'protected_namespace')


async def create_resource(app,ctx,request,tx, *, parent,type,name=None,body=None,media_type='text/markdown',
                          relations=(),mode=None,resource_id=None,author=None,content_signature=None,revision_id=None):
    parent=await tx.resource(parent)
    require(parent.state=='active','ancestor_inactive')
    require(app.registry.resource_type(parent.type,parent.type_version).container,'not_a_container')
    app.registry.resource_type(type,1)
    if parent.id=='t_last_will':
        require(request.operation=='identity.legacy_put' and type=='legacy_directive',
                'legacy_directive_only')
    if type=='post':
        name=(name or new_id('p'))
        if not name.endswith('.md'):
            name += '.md'
    name=validate_name(name or new_id('p'))
    if parent.type=='user' and name in {'SOUL.md','AGENTS.md','notes'}:
        require(request.operation in {'identity.personal_put','identity.note_put'},
                'personal_managed_resource')
    if (parent.name=='notes' and parent.parent is not None and
            (await tx.resource(parent.parent)).type=='user'):
        require(request.operation=='identity.note_put','personal_managed_resource')
    await protect_namespace(app,ctx,request,tx,parent,name)
    principal=ctx.principal
    subject=await tx.subject(principal.subject)
    policy=tx.setting('policy:'+parent.id,{})
    container=app.registry.resource_type(type,1).container
    default=policy.get(type+'_mode',policy.get('topic_mode' if container else 'post_mode','1777' if container else '0644'))
    mode=int(default,8) if mode is None else mode
    if container and parent.mode&SETGID:
        mode|=SETGID
    rid=resource_id or new_id()
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',rid)),'invalid_resource_id')
    resource=Resource(id=rid,type=type,type_version=1,name=name,parent=parent.id,owner=subject.resource_id,
        group=inherit_group(parent,subject.primary_group),mode=mode,generation=0,revision=None,state='active',
        created_at=ctx.now,created_by=principal.actor,modified_at=ctx.now,modified_by=principal.actor)
    await tx.insert(resource)
    if body is not None:
        resource=await revise_resource(app,ctx,request,tx,resource,body,media_type,relations=relations,
                                      author=author,signature=content_signature,revision_id=revision_id)
    return resource


async def revise_resource(app,ctx,request,tx,resource,body,media_type='text/markdown', *, relations=(),author=None,
                          signature=None,revision_id=None):
    from msg.core.models import BlobRef
    if isinstance(body,BlobRef):
        blob=body
    else:
        blob=await app.contents.put_bytes(body.encode('utf-8') if isinstance(body,str) else body,media_type)
    for relation in relations:
        spec=app.registry.resource_type(resource.type,resource.type_version)
        require(relation.type in spec.relations,'invalid_relation_type')
        await check_access(app,ctx,request,tx,relation.target.id,'read')
        if relation.target.revision is not None:
            await tx.revision(relation.target)
    rid=revision_id or new_id('v')
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',rid)),'invalid_revision_id')
    content_time=ctx.now
    if signature is not None:
        require('content_created_at' in request.arguments,'content_timestamp_required')
        content_time=parse_time(request.arguments['content_created_at'])
        require(abs((content_time-ctx.now).total_seconds())<=300,'content_timestamp_out_of_range')
    revision=Revision(format_version=1,id=rid,resource_id=resource.id,parents=(resource.revision,) if resource.revision else (),
        content=blob,relations=tuple(relations),actor=ctx.principal.actor,subject=ctx.principal.subject,
        author=author or ctx.principal.subject,created_at=content_time,manifest_digest='')
    custodial=(ctx.principal.method=='token' and
               (await tx.subject(ctx.principal.subject)).kind=='custodial')
    if custodial:
        require(signature is None,'custodial_content_signature_forbidden')
        revision=replace(revision,signature_source='custodial',source_kind='operation',
                         source_version=request.contract_version,source_digest=request.payload_digest)
    body_to_sign={k:v for k,v in wire(revision).items() if k not in {'manifest_digest','signature'}}
    revision=replace(revision,manifest_digest=digest(body_to_sign))
    if signature is not None:
        sig=decode(Signature,signature)
        credential=await tx.credential(ctx.principal.credential_id)
        require(sig.key_id==credential.id,'content_signer_mismatch')
        verify(credential.verifier,canonical(body_to_sign),sig,purpose='revision')
        revision=replace(revision,signature=sig)
    elif custodial:
        from msg.security.vault import open_signer
        signer=open_signer(app,tx,ctx.principal.subject)
        primary=tx.one('SELECT key_id FROM identity_keys WHERE subject=? AND is_primary=1',
                       (ctx.principal.subject,))
        require(primary is not None and primary[0]==signer.key_id,'custodial_vault_key_mismatch')
        revision=replace(revision,signature=signer.sign(canonical(body_to_sign),purpose='revision'))
    await app.contents.pin(blob,rid)
    ancestors=await tx.ancestors(resource.id)
    topic=next((p.id for p in reversed(ancestors) if p.type=='topic'),ROOT_SPACE)
    await app.contents.commit_revision(topic,revision)
    await tx.append_revision(revision)
    updated=replace(resource,generation=resource.generation+1,revision=rid,modified_at=ctx.now,modified_by=ctx.principal.actor)
    await tx.replace(updated,resource.generation)
    if media_type.startswith('text/') and blob.size<=1048576:
        text=(await app.contents.read_bytes(blob)).decode('utf-8',errors='replace')
        tx.execute('INSERT INTO projections VALUES (?,?) ON CONFLICT(resource_id) DO UPDATE SET text=excluded.text',
                   (resource.id,text),write=True)
    return updated


def output_for(resource,**data):
    return HandlerOutput(resources=(ResourceRef(id=resource.id,revision=resource.revision),),
                         data={'generation':resource.generation,**data})


async def assert_generation(request,resource):
    values=dict(request.expected_generations)
    require(resource.id in values,'expected_generation_required')
    require(values[resource.id]==resource.generation,'generation_conflict',
            details={'id':resource.id,'generation':resource.generation,'revision':resource.revision})


async def topic_policy(tx,resource):
    topic=resource if resource.type=='topic' else await tx.resource(resource.parent)
    return tx.setting('policy:'+topic.id,{})


def registration(app,name,dependencies=()):
    from msg.core.models import OperationSpec,PluginManifest,ResourceRef
    from msg.plugins.schemas import OUTPUT
    operations=[]
    output_ref=ResourceRef(id='schema:operation-result')
    if output_ref.id not in app.registry._schemas:
        app.registry.add_schema(output_ref,OUTPUT)
    def operation(opname,schema, *, effect='transaction',requirements=no_requirements,signature=False,version=1):
        def decorate(handler):
            ref=ResourceRef(id='schema:'+opname+':'+str(version))
            app.registry.add_schema(ref,schema)
            operations.append(OperationSpec(name=opname,version=version,input_schema=ref,output_schema=output_ref,
                effect=effect,entries=frozenset({'network','worker'}),require_signature=signature,
                requirements=requirements,handler=handler))
            return handler
        return decorate
    def finish(resource_types=()):
        manifest=PluginManifest(name=name,version='1',dependencies=tuple(dependencies),
            resource_types=tuple(resource_types),capabilities=(),operations=tuple(operations),migrations=())
        app.registry.add(manifest)
    return operation,finish
