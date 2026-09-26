"""Controlled identity, organization and certificate facts."""
from __future__ import annotations
import hashlib
import hmac
import os
import re
from dataclasses import replace
from datetime import timedelta
from msg.constants import *
from msg.core.codec import b64,unb64,canonical,decode,digest,wire,loads,parse_time
from msg.core.errors import Failure,require
from msg.core.models import (
    Subject,Organization,Membership,Credential,Resource,ResourceRef,ResourceTypeSpec,
    CapabilityGrant,Certificate,CertificateRequest,IssuancePolicy,Signature,CertificateRequestState,
    Scope,HandlerOutput,EmailSettings,EffectJob,Event,AuditEvent,AccessRequirement,
)
from msg.plugins.common import registration,resolve,operation_id,check_access,new_id,create_resource,output_for
from msg.plugins.schemas import *
from msg.security.crypto import key_id,subject_id,verify
from msg.security.certificates import ONLINE_ISSUABLE_CAPABILITIES,sign_certificate,csr_body,verify_csr
from msg.security.policy import scope_subset,constraints_subset


async def make_user(app,tx,ctx,id,handle,kind):
    require(re.fullmatch(r'[a-z][a-z0-9-]{1,40}',handle) is not None and handle not in {'root','online-ca'},'invalid_handle')
    resource=Resource(id=id,type='user',type_version=1,name='@'+handle,parent=ROOT_SPACE,owner=id,group=PUBLIC_GROUP,
        mode=0o755,generation=0,revision=None,state='active',created_at=ctx.now,created_by=id,
        modified_at=ctx.now,modified_by=id)
    await tx.insert(resource)
    await tx.update_identity(Subject(resource_id=id,kind=kind,primary_group=PUBLIC_GROUP,auth_version=0),-1)
    if kind=='registered':
        await set_member(tx,PUBLIC_GROUP,id,'member')
    for name,mode in (('keys',0o555),('certificates',0o555),('files',0o700),('keystore',0o700)):
        await tx.insert(Resource(id='r_'+digest((id,name))[7:39],type='topic',type_version=1,name=name,parent=id,
            owner=id,group=PUBLIC_GROUP,mode=mode,generation=0,revision=None,state='active',
            created_at=ctx.now,created_by=id,modified_at=ctx.now,modified_by=id))
    return resource


async def set_member(tx,org,subject,role):
    organization=await tx.organization(org)
    member=tx.one('SELECT generation FROM memberships WHERE org=? AND subject=?',(org,subject))
    generation=member[0] if member else -1
    await tx.update_identity(Membership(organization_id=org,subject_id=subject,role=role,version=generation+1),generation)
    await tx.update_identity(replace(organization,membership_version=organization.membership_version+1),organization.membership_version)


async def certificate_resource(tx,cert,now):
    await tx.insert(Resource(id=cert.resource_id,type='certificate',type_version=1,name=cert.resource_id,parent=CERT_SPACE,
        owner=cert.subject_id,group=PUBLIC_GROUP,mode=0o444,generation=0,revision=None,state='active',
        created_at=now,created_by=cert.issuer_id,modified_at=now,modified_by=cert.issuer_id))


async def issue_online(app,tx,subject,key,ctx,request, *, grants=None,kind='identity',sources=(),depth=0,
                       ttl=None,authority_source=None):
    issuer=await app.online_issuer(tx)
    now=ctx.now
    lifetime=app.settings.base_certificate_ttl if ttl is None else ttl
    require(0<lifetime<=issuer.issuance.max_cert_ttl_seconds,'certificate_ttl_escalation')
    if kind=='delegation':
        require(bool(sources),'authority_source_required')
        for source in sources:
            fact=tx.setting('delegation:'+source.id)
            require(fact is not None and parse_time(fact['expires_at'])>now,'authority_source_expired')
            lifetime=min(lifetime,(parse_time(fact['expires_at'])-now).total_seconds())
    cert=Certificate(resource_id=new_id('cert'),serial=new_id('serial'),subject_id=subject,key_id=key,
        issuer_id=ONLINE_CA,parent_certificate_id=issuer.resource_id,authority_sources=tuple(sources),kind=kind,
        grants=tuple(app.base_grants() if grants is None else grants),not_before=now,
        expires_at=min(now+timedelta(seconds=lifetime),issuer.expires_at),
        target_service=app.settings.service_url,delegation_depth=depth,issuance=None,
        signature=Signature(key_id=app.online_signer.key_id,algorithm='ed25519',value=b''))
    cert=sign_certificate(cert,app.online_signer)
    await certificate_resource(tx,cert,now)
    await app.certificates.validate(cert.resource_id,tx,certificate=cert)
    await tx.register_certificate(cert,None,0)
    source=authority_source or ({'kind':'delegation','id':sources[0].id,'revision':sources[0].revision}
                                if sources else {'kind':'possession_proof','key_id':key,
                                                'request_id':request.request_id})
    authority=(tuple(sources) if sources else
               (ResourceRef(id=source['certificate_id']),) if source.get('certificate_id') else
               (ResourceRef(id=subject),))
    event=Event(id=new_id('audit'),type='cert.auto.issue',time=now,request_id=request.request_id,
                actor=ctx.principal.actor,subject=subject,resources=(ResourceRef(id=cert.resource_id),),
                data={'automatic':True,'policy_version':1,'issuer':ONLINE_CA,
                      'issuer_certificate_id':issuer.resource_id,'signing_key_id':app.online_signer.key_id,
                      'certificate_id':cert.resource_id,'authority_source':source,
                      'grant_digest':digest(cert.grants)})
    await tx.append_audit(AuditEvent(event=event,authority=authority,before_digest=None,
        after_digest=digest(cert),previous_digest=None,entry_digest='',result='issued'))
    return cert


async def controlled_owner(app,ctx,request,tx,target=None):
    target=target or ctx.principal.subject
    require(ctx.principal.actor==ctx.principal.subject==target,'identity_owner_required')
    require(target!=ROOT_SUBJECT,'local_only')
    await app.authorizer.require_base(ctx.principal,operation_id(request),target,tx)
    return await tx.subject(target)


async def validate_ceiling(app,ctx,tx,grants):
    for grant in grants:
        await app.certificates.validate_grant(grant,tx)
        require(any([g.capability==grant.capability and g.version==grant.version and
                     grant.operations<=g.operations and await scope_subset(grant.scope,g.scope,tx)
                     and constraints_subset(grant.constraints,g.constraints) for g in ctx.principal.ceiling]),
                'credential_ceiling_escalation')


def install(app):
    op,finish=registration(app,'identity')

    @op('identity.register',obj({'handle':STRING,'public_key':BYTES},('handle','public_key')),signature=True)
    async def register(ctx,request,tx):
        await app.online_issuer(tx)
        public=unb64(request.arguments['public_key'],limit=32)
        user=await make_user(app,tx,ctx,ctx.principal.subject,request.arguments['handle'],'registered')
        credential=Credential(id=key_id(public),subject_id=user.id,kind='signing_key',verifier=public,
            ceiling=app.primary_ceiling(),not_before=ctx.now,expires_at=None,revoked_at=None)
        await tx.save_credential(credential,0)
        cert=await issue_online(app,tx,user.id,credential.id,ctx,request)
        return HandlerOutput(resources=(ResourceRef(id=user.id),),data={'subject_id':user.id,'key_id':credential.id,
            'certificate_id':cert.resource_id,'handle':request.arguments['handle']})

    @op('identity.certificate_renew',obj({'grants':GRANTS,'ttl':{'type':'integer','minimum':1}}),signature=True)
    async def certificate_renew(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        require(subject.kind=='registered','registered_identity_required')
        await app.online_issuer(tx)
        candidates=[]
        for (raw,) in tx.execute('SELECT body FROM certificates WHERE subject=?',(subject.resource_id,)):
            source=decode(Certificate,loads(raw))
            if source.kind!='identity' or source.key_id!=ctx.principal.credential_id:
                continue
            try:
                await app.certificates.validate(source.resource_id,tx)
            except Failure as exc:
                if exc.code in {'certificate_revoked','certificate_expired','certificate_key_revoked',
                                'credential_revoked','authority_source_inactive','authority_source_changed',
                                'authority_source_expired','authority_source_lost'}:
                    continue
                raise
            candidates.append(source)
        require(bool(candidates),'renewal_source_required')
        candidates.sort(key=lambda source:(source.not_before,source.resource_id))
        source=candidates[0]
        grants=tuple(decode(CapabilityGrant,g) for g in request.arguments['grants']) if 'grants' in request.arguments else source.grants
        ttl=request.arguments.get('ttl',int((source.expires_at-source.not_before).total_seconds()))
        require(ttl<=(source.expires_at-source.not_before).total_seconds(),'renewal_ttl_exceeded')
        for grant in grants:
            await app.certificates.validate_grant(grant,tx)
            require(any([g.capability==grant.capability and g.version==grant.version and
                        grant.operations<=g.operations and await scope_subset(grant.scope,g.scope,tx) and
                        constraints_subset(grant.constraints,g.constraints) for g in source.grants]),
                    'renewal_scope_exceeded')
        cert=await issue_online(app,tx,subject.resource_id,ctx.principal.credential_id,ctx,request,
                                grants=grants,ttl=ttl,
                                authority_source={'kind':'certificate','certificate_id':source.resource_id})
        return HandlerOutput(data={'subject_id':subject.resource_id,'certificate_id':cert.resource_id,
                                   'expires_at':wire(cert.expires_at)})

    @op('identity.temporary',obj({'nonce':BYTES},('nonce',)))
    async def temporary(ctx,request,tx):
        token=app.issued_token(request,ctx.principal.subject)
        user=await make_user(app,tx,ctx,ctx.principal.subject,'tmp-'+ctx.principal.subject[-24:],'temporary')
        credential=Credential(id=ctx.principal.credential_id,subject_id=user.id,kind='token',
            verifier=hashlib.sha256(token).digest(),ceiling=app.temporary_ceiling(),not_before=ctx.now,
            expires_at=ctx.now+timedelta(seconds=app.settings.temporary_ttl),revoked_at=None)
        await tx.save_credential(credential,0)
        return HandlerOutput(resources=(ResourceRef(id=user.id),),data={'subject_id':user.id,
            'credential_id':credential.id,'expires_at':wire(credential.expires_at)})

    @op('identity.token_rotate',obj({'nonce':BYTES},('nonce',)))
    async def token_rotate(ctx,request,tx):
        await controlled_owner(app,ctx,request,tx)
        old=await tx.credential(ctx.principal.credential_id)
        require(old.kind=='token','token_required')
        require(len(unb64(request.arguments['nonce']))>=24,'invalid_bootstrap_nonce')
        token=app.issued_token(request,ctx.principal.subject)
        subject=await tx.subject(ctx.principal.subject)
        credential=Credential(id='t_'+digest((request.request_id,ctx.principal.subject))[7:39],subject_id=subject.resource_id,
            kind='token',verifier=hashlib.sha256(token).digest(),ceiling=old.ceiling,not_before=ctx.now,
            expires_at=ctx.now+timedelta(seconds=app.settings.temporary_ttl),revoked_at=None)
        await tx.save_credential(credential,subject.auth_version)
        await tx.save_credential(replace(old,revoked_at=ctx.now),subject.auth_version)
        return HandlerOutput(data={'subject_id':subject.resource_id,'credential_id':credential.id,
            'previous_credential':old.id,'expires_at':wire(credential.expires_at)})

    @op('identity.upgrade',obj({'handle':STRING,'public_key':BYTES,'possession_proof':SIGNATURE},('handle','public_key','possession_proof')))
    async def upgrade(ctx,request,tx):
        await app.online_issuer(tx)
        subject=await controlled_owner(app,ctx,request,tx)
        require(subject.kind=='temporary','not_temporary')
        a=request.arguments
        public=unb64(a['public_key'],limit=32)
        verify(public,canonical({'subject_id':subject.resource_id,'public_key':a['public_key'],'handle':a['handle']}),
               decode(Signature,a['possession_proof']),purpose='upgrade')
        require(re.fullmatch(r'[a-z][a-z0-9-]{1,40}',a['handle']) is not None and a['handle'] not in {'root','online-ca'},'invalid_handle')
        resource=await tx.resource(subject.resource_id)
        updated=replace(resource,name='@'+a['handle'],generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        await tx.update_identity(replace(subject,kind='registered',auth_version=subject.auth_version+1),subject.auth_version)
        await set_member(tx,PUBLIC_GROUP,subject.resource_id,'member')
        credential=Credential(id=key_id(public),subject_id=subject.resource_id,kind='signing_key',verifier=public,
            ceiling=app.primary_ceiling(),not_before=ctx.now,expires_at=None,revoked_at=None)
        await tx.save_credential(credential,subject.auth_version+1)
        certificate=await issue_online(app,tx,subject.resource_id,credential.id,ctx,request)
        return HandlerOutput(resources=(ResourceRef(id=subject.resource_id),),data={'subject_id':subject.resource_id,
            'key_id':credential.id,'certificate_id':certificate.resource_id,'handle':a['handle']})

    @op('identity.key_add',obj({'public_key':BYTES,'possession_proof':SIGNATURE,'ceiling':GRANTS},
                               ('public_key','possession_proof','ceiling')),signature=True)
    async def key_add(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        public=unb64(request.arguments['public_key'],limit=32)
        verify(public,canonical({'subject_id':subject.resource_id,'public_key':b64(public)}),
               decode(Signature,request.arguments['possession_proof']),purpose='key-add')
        ceiling=tuple(decode(CapabilityGrant,g) for g in request.arguments['ceiling'])
        await validate_ceiling(app,ctx,tx,ceiling)
        require(tx.one('SELECT id FROM credentials WHERE id=?',(key_id(public),)) is None,'key_exists')
        credential=Credential(id=key_id(public),subject_id=subject.resource_id,kind='signing_key',verifier=public,
            ceiling=ceiling,not_before=ctx.now,expires_at=None,revoked_at=None)
        await tx.save_credential(credential,subject.auth_version)
        await tx.update_identity(replace(subject,auth_version=subject.auth_version+1),subject.auth_version)
        cert=await issue_online(app,tx,subject.resource_id,credential.id,ctx,request,
                                grants=tuple(g for g in ceiling
                                             if g.capability in ONLINE_ISSUABLE_CAPABILITIES))
        return HandlerOutput(data={'key_id':credential.id,'certificate_id':cert.resource_id})

    @op('identity.key_revoke',obj({'key_id':IDENTIFIER},('key_id',)),signature=True)
    async def key_revoke(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        credential=await tx.credential(request.arguments['key_id'])
        require(credential.subject_id==subject.resource_id,'credential_owner_required')
        active=[decode(Credential,loads(r[0])) for r in tx.rows('SELECT body FROM credentials WHERE subject=?',(subject.resource_id,))]
        require(credential.kind!='signing_key' or any(c.kind=='signing_key' and c.revoked_at is None and c.id!=credential.id for c in active),
                'last_signing_key')
        await tx.save_credential(replace(credential,revoked_at=ctx.now),subject.auth_version)
        await tx.update_identity(replace(subject,auth_version=subject.auth_version+1),subject.auth_version)
        return HandlerOutput(data={'key_id':credential.id,'revoked':True})

    @op('identity.token_create',obj({'nonce':BYTES,'ceiling':GRANTS,'ttl':{'type':'integer','minimum':1,'maximum':86400}},
                                    ('nonce','ceiling','ttl')),signature=True)
    async def token_create(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        ceiling=tuple(decode(CapabilityGrant,g) for g in request.arguments['ceiling'])
        await validate_ceiling(app,ctx,tx,ceiling)
        require(len(unb64(request.arguments['nonce']))>=24,'invalid_bootstrap_nonce')
        credential=Credential(id='t_'+digest((request.request_id,subject.resource_id))[7:39],subject_id=subject.resource_id,
            kind='token',verifier=hashlib.sha256(app.issued_token(request,subject.resource_id)).digest(),ceiling=ceiling,
            not_before=ctx.now,expires_at=ctx.now+timedelta(seconds=request.arguments['ttl']),revoked_at=None)
        await tx.save_credential(credential,subject.auth_version)
        return HandlerOutput(data={'subject_id':subject.resource_id,'credential_id':credential.id,'expires_at':wire(credential.expires_at)})

    @op('group.create',obj({'name':STRING},('name',)),signature=True)
    async def group_create(ctx,request,tx):
        await app.authorizer.require_base(ctx.principal,operation_id(request),ctx.principal.subject,tx)
        name=request.arguments['name']
        require(re.fullmatch(r'[a-z][a-z0-9-]{1,40}',name) is not None and name not in {'public','admins'},'invalid_group_name')
        rid=new_id('g')
        r=Resource(id=rid,type='organization',type_version=1,name='&'+name,parent=ROOT_SPACE,
            owner=ctx.principal.subject,group=rid,mode=0o2775,generation=0,revision=None,state='active',
            created_at=ctx.now,created_by=ctx.principal.actor,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.insert(r)
        await tx.update_identity(Organization(resource_id=rid,membership_version=0),-1)
        await set_member(tx,rid,ctx.principal.subject,'admin')
        return output_for(r)

    async def group_change(ctx,request,tx):
        org=await resolve(tx,request.arguments['group'])
        resource=await tx.resource(org)
        organization=await tx.organization(org)
        member=tx.one('SELECT body FROM memberships WHERE org=? AND subject=?',(org,ctx.principal.subject))
        role=decode(Membership,loads(member[0])).role if member else None
        allowed=(resource.owner==ctx.principal.subject or role=='admin') and await app.authorizer.ordinary(ctx.principal,operation_id(request),org,tx)
        if not allowed:
            allowed=await app.authorizer.has(ctx.principal,'group.manage_override',operation_id(request),org,tx)
        require(allowed,'group_admin_required')
        target=await resolve(tx,request.arguments['subject'])
        await tx.subject(target)
        if organization.builtin=='public':
            require(await app.authorizer.has(ctx.principal,'system.namespace',operation_id(request),org,tx),'protected_group')
        await app.authorizer._ceiling(ctx.principal,operation_id(request),org,tx)
        action=request.operation
        if action=='group.member.remove':
            require(target!=resource.owner,'cannot_remove_group_owner')
            tx.execute('DELETE FROM memberships WHERE org=? AND subject=?',(org,target),write=True)
            tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)+1)
            await tx.update_identity(replace(organization,membership_version=organization.membership_version+1),organization.membership_version)
        else:
            await set_member(tx,org,target,'admin' if action=='group.admin.add' else 'member')
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'subject_id':target,'action':action})
    for name in ('group.member.add','group.member.remove','group.admin.add','group.admin.remove'):
        op(name,obj({'group':IDENTIFIER,'subject':IDENTIFIER},('group','subject')),signature=True)(group_change)

    @op('cert.request',obj({'requested_issuer':IDENTIFIER,'public_key':BYTES,'kind':{'enum':['identity','capability','ca']},
        'grants':GRANTS,'issuance':ISSUANCE,'requested_ttl_seconds':{'type':'integer','minimum':1},
        'delegation_depth':INTEGER,'possession_proof':SIGNATURE},
        ('requested_issuer','public_key','kind','grants','requested_ttl_seconds','possession_proof')),signature=True)
    async def cert_request(ctx,request,tx):
        await app.authorizer.require_base(ctx.principal,operation_id(request),ctx.principal.subject,tx)
        a=request.arguments
        issuer=await resolve(tx,a['requested_issuer'])
        require((await tx.resource(issuer)).type=='user','invalid_issuer')
        public=unb64(a['public_key'],limit=32)
        grants=tuple(decode(CapabilityGrant,g) for g in a['grants'])
        for grant in grants:
            await app.certificates.validate_grant(grant,tx)
        issuance=decode(IssuancePolicy,a['issuance']) if a.get('issuance') is not None else None
        require((a['kind']=='ca')==(issuance is not None),'ca_issuance_required')
        if issuance:
            for grant in issuance.issue_grants:
                await app.certificates.validate_grant(grant,tx)
        csr=CertificateRequest(resource_id=new_id('csr'),applicant=ctx.principal.subject,subject_id=ctx.principal.subject,
            requested_issuer=issuer,public_key=public,kind=a['kind'],grants=grants,issuance=issuance,
            requested_ttl_seconds=a['requested_ttl_seconds'],target_service=app.settings.service_url,
            delegation_depth=a.get('delegation_depth',0),authority_sources=(),request_digest='',
            possession_proof=decode(Signature,a['possession_proof']))
        csr=replace(csr,request_digest=digest(csr_body(csr)))
        verify_csr(csr)
        # A CSR-only key is a verifier, not an unrestricted login key.
        existing=tx.one('SELECT subject FROM credentials WHERE id=?',(key_id(public),))
        require(existing is None or existing[0]==ctx.principal.subject,'key_owner_mismatch')
        if existing is None:
            subject=await tx.subject(ctx.principal.subject)
            await tx.save_credential(Credential(id=key_id(public),subject_id=subject.resource_id,kind='signing_key',
                verifier=public,ceiling=(),not_before=ctx.now,expires_at=None,revoked_at=None),subject.auth_version)
        await tx.insert(Resource(id=csr.resource_id,type='csr',type_version=1,name=csr.resource_id,parent=CSR_SPACE,
            owner=ctx.principal.subject,group=PUBLIC_GROUP,mode=0o600,generation=0,revision=None,state='active',
            created_at=ctx.now,created_by=ctx.principal.actor,modified_at=ctx.now,modified_by=ctx.principal.actor))
        await tx.save_csr(csr)
        return HandlerOutput(resources=(ResourceRef(id=csr.resource_id),),data={'csr_id':csr.resource_id,
            'request_digest':csr.request_digest,'status':'pending'})

    @op('cert.get',obj({'id':IDENTIFIER},('id',)),effect='read')
    async def cert_get(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        r=await tx.resource(rid)
        if r.type=='csr':
            return HandlerOutput(data={'request':wire(await tx.csr(rid)),'state':wire(await tx.csr_state(rid))})
        require(r.type=='certificate','not_a_certificate')
        return HandlerOutput(data={'certificate':wire(await tx.certificate(rid)),'revoked':await tx.certificate_revoked(rid)})

    @op('cert.cancel',obj({'id':IDENTIFIER},('id',)),signature=True)
    async def cert_cancel(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        csr=await tx.csr(rid)
        require(csr.applicant==ctx.principal.subject,'csr_owner_required')
        await app.authorizer.require_base(ctx.principal,operation_id(request),rid,tx)
        state=await tx.csr_state(rid)
        await tx.transition_csr(replace(state,status='cancelled',generation=state.generation+1),state.generation)
        return HandlerOutput(resources=(ResourceRef(id=rid),),data={'status':'cancelled'})

    @op('cert.publish',obj({'csr_id':IDENTIFIER,'certificate':{'type':'object'}},('csr_id','certificate')),signature=True)
    async def cert_publish(ctx,request,tx):
        csr=await tx.csr(request.arguments['csr_id'])
        state=await tx.csr_state(csr.resource_id)
        require(state.status=='pending','csr_not_pending')
        cert=decode(Certificate,request.arguments['certificate'])
        require(cert.issuer_id==ctx.principal.actor==ctx.principal.subject and cert.issuer_id!=ROOT_SUBJECT,'issuer_mismatch')
        await app.authorizer._ceiling(ctx.principal,operation_id(request),csr.resource_id,tx)
        require(await app.authorizer.has(ctx.principal,'cert.issue',operation_id(request),csr.resource_id,tx),'certificate_issue_required')
        await certificate_resource(tx,cert,ctx.now)
        await app.certificates.validate_publication(cert,csr,tx)
        await tx.register_certificate(cert,csr.resource_id,state.generation)
        credential=await tx.credential(cert.key_id)
        if not credential.ceiling:
            subject=await tx.subject(credential.subject_id)
            await tx.save_credential(replace(credential,ceiling=cert.grants),subject.auth_version)
        return HandlerOutput(resources=(ResourceRef(id=cert.resource_id),),data={'certificate_id':cert.resource_id,'status':'issued'})

    @op('cert.revoke',obj({'id':IDENTIFIER,'reason':STRING},('id','reason')),signature=True)
    async def cert_revoke(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        certificate=await tx.certificate(rid)
        require(certificate.subject_id!=ROOT_SUBJECT,'local_only')
        require(await app.authorizer.has(ctx.principal,'cert.revoke',operation_id(request),rid,tx),'certificate_revoke_required')
        event=Event(id=new_id('e'),type='cert.revoke',time=ctx.now,request_id=request.request_id,
                    actor=ctx.principal.actor,subject=ctx.principal.subject,resources=(ResourceRef(id=rid),),
                    data={'reason':request.arguments['reason']})
        audit=AuditEvent(event=event,authority=tuple(ResourceRef(id=c) for c in ctx.principal.certificates),
            before_digest=digest(certificate),after_digest=None,previous_digest=None,entry_digest='',result='revoked')
        await tx.revoke_certificate(rid,audit)
        return HandlerOutput(resources=(ResourceRef(id=rid),),data={'revoked':True})

    @op('identity.delegate',obj({'grantee':IDENTIFIER,'key_id':IDENTIFIER,'grants':GRANTS,
        'ttl':{'type':'integer','minimum':1,'maximum':2592000},'depth':{'type':'integer','minimum':0,'maximum':8}},
        ('grantee','key_id','grants','ttl')),signature=True)
    async def delegate(ctx,request,tx):
        await app.online_issuer(tx)
        a=request.arguments
        grantee=await resolve(tx,a['grantee'])
        key=await tx.credential(a['key_id'])
        require(key.subject_id==grantee and key.kind=='signing_key' and key.revoked_at is None,'invalid_delegate_key')
        grants=tuple(decode(CapabilityGrant,g) for g in a['grants'])
        parent_cert=None
        if ctx.principal.actor!=ctx.principal.subject:
            candidates=[await app.certificates.validate(c,tx) for c in ctx.principal.certificates]
            candidates=[c for c in candidates if c.kind=='delegation' and c.delegation_depth>0]
            require(bool(candidates),'redelegation_forbidden')
            parent_cert=candidates[0]
            require(a.get('depth',0)<parent_cert.delegation_depth,'delegation_depth_exceeded')
        for grant in grants:
            require(grant.capability in app.base_capability_names,'special_delegation_requires_ca')
            await app.certificates.validate_grant(grant,tx)
            resource=await tx.resource(grant.scope.resource_id)
            require(resource.owner==ctx.principal.subject,'delegation_owner_required')
            await app.authorizer._ceiling(ctx.principal,operation_id(request),resource.id,tx)
            if parent_cert:
                require(any([g.capability==grant.capability and grant.operations<=g.operations and
                    await scope_subset(grant.scope,g.scope,tx) for g in parent_cert.grants]),'redelegation_scope')
        fact={'grantor':ctx.principal.subject,'grantee':grantee,'grants':wire(grants),
              'expires_at':wire(ctx.now+timedelta(seconds=a['ttl'])),
              'parent_certificate':parent_cert.resource_id if parent_cert else None}
        resource=await create_resource(app,ctx,request,tx,parent=ctx.principal.subject,type='delegation',
            name=new_id('delegation'),body=canonical(fact),media_type='application/json',mode=0o600)
        tx.set_setting('delegation:'+resource.id,fact)
        cert=await issue_online(app,tx,grantee,key.id,ctx,request,grants=grants,kind='delegation',
                               sources=(ResourceRef(id=resource.id,revision=resource.revision),),depth=a.get('depth',0))
        return HandlerOutput(resources=(ResourceRef(id=resource.id,revision=resource.revision),),
                             data={'certificate_id':cert.resource_id,'grantee':grantee})

    @op('identity.delegation_revoke',obj({'id':IDENTIFIER},('id',)),signature=True)
    async def delegation_revoke(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        resource=await tx.resource(rid)
        require(resource.type=='delegation' and resource.owner==ctx.principal.subject,'delegation_owner_required')
        await app.authorizer._ceiling(ctx.principal,operation_id(request),rid,tx)
        await tx.replace(replace(resource,state='archived',generation=resource.generation+1,
            modified_at=ctx.now,modified_by=ctx.principal.actor),resource.generation)
        return HandlerOutput(resources=(ResourceRef(id=rid),),data={'revoked':True})

    @op('identity.email_set',obj({'address':STRING},('address',)),signature=True)
    async def email_set(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        address=request.arguments['address']
        require(len(address)<=254 and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',address) is not None and '\r' not in address and '\n' not in address,
                'invalid_email')
        row=tx.one('SELECT generation FROM emails WHERE subject=?',(subject.resource_id,))
        await tx.update_identity(EmailSettings(subject_id=subject.resource_id,address=address,verified_at=None),row[0] if row else -1)
        token=os.urandom(32)
        tx.execute('INSERT INTO email_challenges VALUES (?,?,?) ON CONFLICT(subject) DO UPDATE SET digest=excluded.digest,expires=excluded.expires',
            (subject.resource_id,digest(token),wire(ctx.now+timedelta(minutes=15))),write=True)
        if app.settings.server.mail is not None:
            from msg.plugins.communication import event_id
            job=EffectJob(id=new_id('job'),event_id=event_id(request,subject.resource_id),kind='mail',dedupe_key='email-verify:'+subject.resource_id+':'+request.request_id,
                principal=ctx.principal,operation=request.operation,arguments={'recipient':address,'recipient_subject':subject.resource_id,'verification':True,'subject':'Verify msg email',
                'text':'Verify with msg call identity.email_verify using JSON token: '+b64(token)},state='pending',attempts=0,next_attempt_at=ctx.now,lease_until=None)
            await tx.enqueue(job)
        return HandlerOutput(data={'verified':False,'delivery':'queued' if app.settings.server.mail else 'mail_disabled'})

    @op('identity.email_verify',obj({'token':BYTES},('token',)),signature=True)
    async def email_verify(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        row=tx.one('SELECT digest,expires FROM email_challenges WHERE subject=?',(subject.resource_id,))
        require(row is not None and parse_time(row[1])>ctx.now and
                hmac.compare_digest(row[0],digest(unb64(request.arguments['token']))),'invalid_email_challenge')
        email=tx.one('SELECT generation,body FROM emails WHERE subject=?',(subject.resource_id,))
        settings=decode(EmailSettings,loads(email[1]))
        await tx.update_identity(replace(settings,verified_at=ctx.now),email[0])
        tx.execute('DELETE FROM email_challenges WHERE subject=?',(subject.resource_id,),write=True)
        return HandlerOutput(data={'verified':True})

    @op('identity.email_get',obj(),effect='read',signature=True)
    async def email_get(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        row=tx.one('SELECT body FROM emails WHERE subject=?',(subject.resource_id,))
        return HandlerOutput(data={'email':loads(row[0]) if row else None})

    @op('identity.email_notifications',obj({'events':{'type':'array','items':{'enum':['communication.send','discussion.reply','mention','cert.request','cert.issued']}}},('events',)),signature=True)
    async def email_notifications(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        row=tx.one('SELECT generation,body FROM emails WHERE subject=?',(subject.resource_id,))
        require(row is not None,'email_not_set')
        settings=decode(EmailSettings,loads(row[1]))
        require(settings.verified_at is not None or not request.arguments['events'],'email_not_verified')
        await tx.update_identity(replace(settings,enabled_events=frozenset(request.arguments['events'])),row[0])
        return HandlerOutput(data={'events':list(request.arguments['events'])})

    @op('identity.recover',obj({'subject':IDENTIFIER,'public_key':BYTES,'possession_proof':SIGNATURE},
        ('subject','public_key','possession_proof')),signature=True)
    async def recover(ctx,request,tx):
        target=await resolve(tx,request.arguments['subject'])
        subject=await tx.subject(target)
        require(not subject.local_only and target!=ROOT_SUBJECT,'local_only')
        require(await app.authorizer.has(ctx.principal,'identity.recover',operation_id(request),target,tx),'identity_recovery_required')
        public=unb64(request.arguments['public_key'],limit=32)
        verify(public,canonical({'subject_id':target,'public_key':b64(public)}),
               decode(Signature,request.arguments['possession_proof']),purpose='recover')
        for row in tx.rows('SELECT body FROM credentials WHERE subject=?',(target,)):
            await tx.save_credential(replace(decode(Credential,loads(row[0])),revoked_at=ctx.now),subject.auth_version)
        credential=Credential(id=key_id(public),subject_id=target,kind='signing_key',verifier=public,
            ceiling=app.primary_ceiling(),not_before=ctx.now,expires_at=None,revoked_at=None)
        await tx.save_credential(credential,subject.auth_version)
        await tx.update_identity(replace(subject,auth_version=subject.auth_version+1),subject.auth_version)
        cert=await issue_online(app,tx,target,credential.id,ctx,request)
        return HandlerOutput(resources=(ResourceRef(id=target),),data={'key_id':credential.id,'certificate_id':cert.resource_id})

    all_types=('topic','post','template','file','attachment','tool','user','organization','certificate','csr','delegation','repo','website','keystore','skill')
    types=[ResourceTypeSpec(name=name,version=1,container=name in {'topic','user','organization','repo','website'},
        content_schema=None,operations=frozenset(),relations=frozenset({'reply_to','thread_root','quote','repost','attachment','template'}),
        taggable=name in {'post','topic','repo'}) for name in all_types]
    finish(types)
