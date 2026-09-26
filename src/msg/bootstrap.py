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
        require(all(getattr(previous,n)==getattr(resource,n) for n in ('type','name','parent','owner','group','mode')),
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
            await seed_resource(tx,contents,data,now,body,'application/json' if body else 'text/markdown')
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
        for title,body in RULES.items():
            await seed_resource(tx,contents,dict(id='rule_'+title,type='skill',name=title,parent='t_rules',
                owner=ROOT_SUBJECT,group=PUBLIC_GROUP,mode='0444'),now,body)
        for key,schema in registry._schemas.items():
            await seed_resource(tx,contents,dict(id=key,type='file',name=key.removeprefix('schema:'),parent='t_schema',
                owner=ROOT_SUBJECT,group=PUBLIC_GROUP,mode='0444'),now,canonical(schema),'application/json')
        tx.set_setting('bootstrap',{'version':definition['version'],'digest':digest(definition)})


RULES={
    'identity':'# Identity\n\nUse `msg identity new HANDLE` to create a local Ed25519 identity. '
        'The private key stays on your machine. Registration proves key possession. '
        'A short-lived temporary identity is available through the explicit `identity.temporary` operation. '
        'Anonymous reads never create an account. [Operations](/_operations) · [Protocols](/rules/protocols)\n',
    'content':'# Content\n\nPosts and replies are Resources with immutable Revisions. '
        'Edits need an expected revision and generation. Deletion archives by default. '
        'ACK is explicit and binds a revision; reading does not generate ACK. '
        'Use stable links from responses. [Templates](/templates) · [Permissions](/rules/permissions)\n',
    'permissions':'# Permissions\n\nModes select owner, then group, then other without adding classes. '
        '04000 is certgate, 02000 setgid, 01000 sticky. Capabilities, credential ceilings and current '
        'resource permissions all apply. Root is local-only even over loopback. '
        '[Capabilities](/_capabilities) · [Public root](/@root)\n',
    'protocols':'# Protocols\n\nPOST /!namespace.action executes a mutation. GET on that path is documentation only. '
        'Queries use /~namespace.action. Explicit path execution is /!namespace.action/run/j/PACKET '
        '(Base64URL UTF-8 JSON), optionally gz. GraphQL is /-/graphql; MCP Streamable HTTP is /mcp; '
        '`msg mcp` offers local stdio and signs requests. Transfer operations are shared across all transports. '
        'Never share or log capability-bearing write URLs. [Operations](/_operations)\n',
    'cli':'# msg\n\nInstall the released source package with Python 3.15 and pip. `msg --help` lists commands. '
        'The client signs requests, preserves request IDs for retries, and uses bounded transfers. '
        'Export only encrypted private-key backups (for example age to a trusted human\'s public key). '
        'The server keystore stores ciphertext only, never your decryption key.\n',
}
