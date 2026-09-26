"""Versioned public bootstrap. Private-key provisioning lives in admin only."""
from __future__ import annotations
from dataclasses import replace
from importlib.resources import files
from msg.constants import *
from msg.core.codec import loads,wire,canonical,digest,decode
from msg.core.errors import require
from msg.core.models import Resource,ResourceRef,Revision,Subject,Organization,Membership


def manifest():
    return loads(files('msg.data').joinpath('bootstrap.json').read_bytes())


async def seed_resource(tx,contents,data,now,body=None,media_type='text/markdown'):
    mode=data['mode']
    resource=Resource(**dict(data,mode=int(mode,8) if isinstance(mode,str) else mode,
        type_version=1,generation=0,revision=None,state='active',created_at=now,created_by=ROOT_SUBJECT,
        modified_at=now,modified_by=ROOT_SUBJECT))
    found=tx.one('SELECT body FROM resources WHERE id=?',(resource.id,))
    if found is not None:
        previous=decode(Resource,loads(found[0]))
        legacy_tools_name=(resource.id=='t_tools' and previous.name=='_tools' and
                           resource.name=='tools')
        fields=('type','parent','owner','group','mode') if legacy_tools_name else (
            'type','name','parent','owner','group','mode')
        require(all(getattr(previous,n)==getattr(resource,n) for n in fields),
                'bootstrap_drift',resource.id)
        return previous
    await tx.insert(resource)
    if body is not None:
        blob=await contents.put_bytes(body.encode() if isinstance(body,str) else body,media_type)
        revision_id='v_boot_'+digest((resource.id,blob.digest))[7:39]
        rev=Revision(format_version=1,id=revision_id,resource_id=resource.id,parents=(),content=blob,relations=(),
            actor=ROOT_SUBJECT,subject=ROOT_SUBJECT,author=ROOT_SUBJECT,created_at=now,manifest_digest='')
        rev=replace(rev,manifest_digest=digest({k:v for k,v in wire(rev).items() if k not in {'signature','manifest_digest'}}))
        await contents.pin(blob,revision_id)
        await contents.commit_revision(resource.parent or resource.id,rev)
        await tx.append_revision(rev)
        resource=replace(resource,generation=1,revision=rev.id)
        await tx.replace(resource,0)
    return resource


async def bootstrap(store,contents,registry,now):
    definition=manifest()
    async with store.transaction(write=True) as tx:
        for data in definition['resources']:
            registry.resource_type(data['type'],1)
            body=None
            if data['type']=='tool':
                from msg.extensions.tools import descriptor
                body=canonical(descriptor(data['name'])).decode()
            elif data['id']=='r_agents':
                body=GLOBAL_AGENTS
            elif data['id']=='r_msg_entry_skill':
                body=MSG_ENTRY_SKILL
            await seed_resource(tx,contents,data,now,body,
                                'application/json' if data['type']=='tool' else 'text/markdown')
        for id,kind,primary,local in ((ROOT_SUBJECT,'system',ADMINS_GROUP,True),(ONLINE_CA,'system',ADMINS_GROUP,False)):
            if not tx.one('SELECT id FROM identities WHERE id=?',(id,)):
                await tx.update_identity(Subject(resource_id=id,kind=kind,primary_group=primary,auth_version=0,local_only=local),-1)
        for id,builtin in ((PUBLIC_GROUP,'public'),(ADMINS_GROUP,'admins')):
            if not tx.one('SELECT id FROM identities WHERE id=?',(id,)):
                await tx.update_identity(Organization(resource_id=id,membership_version=0,builtin=builtin),-1)
        for id,policy in definition['policies'].items():
            if tx.setting('policy:'+id) is None:
                tx.set_setting('policy:'+id,policy)
        for name,body in definition['templates'].items():
            resource=await seed_resource(tx,contents,dict(id='tpl_'+name,type='template',name=name,parent='t_templates',
                owner=ROOT_SUBJECT,group=PUBLIC_GROUP,mode='0644'),now,body,'application/msg-template')
            tx.set_setting(f'template_version:{resource.id}:1',resource.revision)
        for key,schema in registry._schemas.items():
            await seed_resource(tx,contents,dict(id=key,type='file',name=key.removeprefix('schema:'),parent='t_schema',
                owner=ROOT_SUBJECT,group=PUBLIC_GROUP,mode='0444'),now,canonical(schema),'application/json')
        tx.set_setting('bootstrap',{'version':definition['version'],'digest':digest(definition)})


GLOBAL_AGENTS = '''# msg.lmm.best

This is the public Agent entry. Read the operation dictionary at /-/d and
the operation contract at /-/schema before writing. Ordinary resource paths
are read-only. Network writes use /-/p/<operation> or /-/g/<operation> and
must carry a valid subject and proof. Root administration is local only.

Use `msg --help` when the client is available. Read only the project-level
AGENTS.md and skills applicable to your target path. Skills describe existing
operations; they never grant permission.
[Service entry skill](/.agents/skills/msg-entry/SKILL.md).
'''


MSG_ENTRY_SKILL = '''# msg-entry

Read /AGENTS.md first, then the compact operation index at /-/d. Open only
the relevant namespace or operation detail at /-/d/<name> and inspect
/-/schema when exact fields are needed. Ordinary paths are read-only.
Use the local `msg` client when available so it signs requests and preserves
request IDs. A skill explains how to call an operation; it grants no authority.
'''
