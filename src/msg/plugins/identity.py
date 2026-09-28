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
from msg.plugins.common import (registration,resolve,operation_id,check_access,new_id,create_resource,
    revise_resource,assert_generation,output_for,validate_name)
from msg.plugins.schemas import *
from msg.core.requests import signing_bytes
from msg.security.crypto import key_id,subject_id,verify
from msg.security.age_keys import public_from_recipient,encryption_key_id
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer
from msg.security.vault import (store_keys,open_signer,seal_upgrade_private,
    server_upgrade_proof)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from msg.security.certificates import ONLINE_ISSUABLE_CAPABILITIES,sign_certificate,csr_body,verify_csr
from msg.security.policy import scope_subset,constraints_subset


SECRET_TEXT=re.compile(r'(?i)(?:-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|AGE-SECRET-KEY-1[A-Z0-9]+'
    r'|\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,}'
    r'|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16})\b'
    r'|\b(?:api[_ -]?key|secret|token|password|private[_ -]?key)\b\s*[:=]\s*\S{12,})')
RULE_BYPASS_TEXT=re.compile(r'(?is)\b(?:ignore|override|bypass|disable|skip)\b.{0,80}'
    r'(?:/_rules|authentication|authorization|certificate|\bCA\b|security|permission)'
    r'|\ballow\s+anonymous\s+writ(?:e|es|ing)\b')


def validate_personal_body(body, *, agents=False, limit=65536):
    """Bounded known-format screening, not arbitrary-secret/NL detection.

    Text is never an authorization source. The English phrase guard is a
    convenience check; nonmatching prose cannot modify the platform rules.
    """
    require(type(body) is str and len(body.encode('utf-8'))<=limit,'personal_text_too_large')
    require(SECRET_TEXT.search(body) is None,'plaintext_secret_forbidden')
    if agents:
        require(RULE_BYPASS_TEXT.search(body) is None,'personal_rules_cannot_relax_platform')


def save_personal_proof(tx,request,resource,subject,kind,now):
    tx.execute('''INSERT INTO personal_revision_proofs
        (revision_id,subject,kind,signature,signed_envelope,created_at) VALUES (?,?,?,?,?,?)''',
        (resource.revision,subject,kind,canonical(request.proof.signature).decode(),
         b64(signing_bytes(request)),wire(now)),write=True)


# Compatibility import; inventory/state invariants live in one module.
from msg.security.custodial_migration import owned_age_inventory as custodial_age_inventory


async def make_user(app,tx,ctx,id,handle,kind):
    require(re.fullmatch(r'[a-z][a-z0-9-]{1,40}',handle) is not None and handle not in {'root','online-ca'},'invalid_handle')
    resource=Resource(id=id,type='user',type_version=1,name='@'+handle,parent=app.namespace_root,owner=id,group=PUBLIC_GROUP,
        mode=0o755,generation=0,revision=None,state='active',created_at=ctx.now,created_by=id,
        modified_at=ctx.now,modified_by=id)
    await tx.insert(resource)
    await tx.update_identity(Subject(resource_id=id,kind=kind,primary_group=PUBLIC_GROUP,auth_version=0),-1)
    for name,mode in (('keys',0o555),('certificates',0o555),('files',0o700),('keystore',0o700)):
        await tx.insert(Resource(id='r_'+digest((id,name))[7:39],type='topic',type_version=1,name=name,parent=id,
            owner=id,group=PUBLIC_GROUP,mode=mode,generation=0,revision=None,state='active',
            created_at=ctx.now,created_by=id,modified_at=ctx.now,modified_by=id))
    return resource


async def set_member(tx,org,subject,role, *, status='active', joined_at=None, invited_by=None):
    organization=await tx.organization(org)
    member=tx.one('SELECT generation FROM memberships WHERE org=? AND subject=?',(org,subject))
    generation=member[0] if member else -1
    await tx.update_identity(Membership(organization_id=org,subject_id=subject,role=role,version=generation+1,
                                       status=status,joined_at=joined_at,invited_by=invited_by),generation)
    await tx.update_identity(replace(organization,membership_version=organization.membership_version+1),organization.membership_version)


def group_member(tx,org,subject):
    row=tx.one('SELECT body FROM memberships WHERE org=? AND subject=?',(org,subject))
    return decode(Membership,loads(row[0])) if row else None


async def remove_group_member(tx,org,subject):
    organization=await tx.organization(org)
    deleted=tx.execute('DELETE FROM memberships WHERE org=? AND subject=?',(org,subject),write=True)
    require(deleted.rowcount==1,'group_membership_not_found')
    tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)+1)
    await tx.update_identity(replace(organization,membership_version=organization.membership_version+1),
                             organization.membership_version)


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
    if authority_source and authority_source.get('kind')=='certificate':
        source=await app.certificates.validate(authority_source['certificate_id'],tx)
        require(source.subject_id==subject and source.key_id==key,'authority_source_invalid')
        lifetime=min(lifetime,(source.expires_at-now).total_seconds())
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
    async def register_legacy(ctx,request,tx):
        # The v1 wire schema and shortcodes remain intact, but it cannot create
        # a compliant self-custody identity without a client-held age key.
        raise Failure('encryption_subkey_required',details={'contract_version':2})

    @op('identity.custodial_create',obj({'handle':STRING,'nonce':BYTES},('handle','nonce')))
    @op('identity.custodial_create',obj({'handle':STRING,'nonce':BYTES,'recovery_secret':BYTES},
                                        ('handle','nonce','recovery_secret')),version=2)
    async def custodial_create(ctx,request,tx):
        token=app.issued_token(request,ctx.principal.subject)
        signer=Ed25519Signer.generate()
        age_identity,recipient=generate_age_key()
        age_public=public_from_recipient(recipient)
        age_id=encryption_key_id(age_public)
        user=await make_user(app,tx,ctx,ctx.principal.subject,
                             request.arguments['handle'],'custodial')
        signing_credential=Credential(id=signer.key_id,subject_id=user.id,kind='signing_key',
            verifier=signer.public_key,ceiling=(),not_before=ctx.now,expires_at=None,revoked_at=None)
        await tx.save_credential(signing_credential,0)
        credential=Credential(id=ctx.principal.credential_id,subject_id=user.id,kind='token',
            verifier=hashlib.sha256(token).digest(),ceiling=app.temporary_ceiling(),
            not_before=ctx.now,expires_at=ctx.now+timedelta(seconds=app.settings.temporary_ttl),
            revoked_at=None)
        await tx.save_credential(credential,0)
        app.record_token_delivery(tx,request,credential,ctx.now)
        tx.execute('INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
                   (signer.key_id,user.id,b64(signer.public_key),wire(ctx.now),None,1),write=True)
        tx.execute('INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
                   (age_id,user.id,recipient,b64(age_public),wire(ctx.now),None,1),write=True)
        store_keys(app,tx,user.id,signer,age_identity,age_id,ctx.now)
        return HandlerOutput(resources=(ResourceRef(id=user.id),),data={
            'subject_id':user.id,'credential_id':credential.id,
            'identity_key_id':signer.key_id,'encryption_key_id':age_id,
            'encryption_recipient':recipient,'expires_at':wire(credential.expires_at),
            'signature_source':'custodial','server_signable':True,'server_decryptable':True})

    @op('identity.custodial_status',obj(),effect='read')
    async def custodial_status(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        require(subject.kind=='custodial','not_custodial')
        row=tx.one('SELECT signing_key_id,encryption_key_id,status FROM custodial_vault WHERE subject=?',
                   (subject.resource_id,))
        require(row is not None and row[2]=='active','custodial_vault_unavailable')
        from msg.security.vault import open_signer,open_age_identity
        open_signer(app,tx,subject.resource_id)
        open_age_identity(app,tx,subject.resource_id)
        return HandlerOutput(data={'subject_id':subject.resource_id,
            'identity_key_id':row[0],'encryption_key_id':row[1],
            'signature_source':'custodial','server_signable':True,
            'server_decryptable':True})

    @op('identity.custodial_upgrade_start',obj({'handle':STRING,'public_key':BYTES,
        'encryption_recipient':STRING,'possession_proof':SIGNATURE},
        ('handle','public_key','encryption_recipient','possession_proof')))
    async def custodial_upgrade_start(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        require(subject.kind=='custodial' and ctx.principal.method=='token',
                'custodial_token_required')
        args=request.arguments
        require(not tx.one('''SELECT id FROM custodial_upgrades WHERE subject=?
            AND (status='pending_rewrap' OR (status='pending' AND expires_at>?))''',
            (subject.resource_id,wire(ctx.now))), 'custodial_upgrade_already_pending')
        require(re.fullmatch(r'[a-z][a-z0-9-]{1,40}',args['handle']) is not None and
                args['handle'] not in {'root','online-ca'},'invalid_handle')
        public=unb64(args['public_key'],limit=32)
        require(len(public)==32,'invalid_public_key')
        age_public=public_from_recipient(args['encryption_recipient'])
        require(public!=age_public,'encryption_key_must_be_independent')
        old_signing=tx.one('SELECT key_id FROM identity_keys WHERE subject=? AND is_primary=1',
                           (subject.resource_id,))
        old_age=tx.one('SELECT key_id FROM encryption_subkeys WHERE subject=? AND is_primary=1',
                       (subject.resource_id,))
        require(old_signing is not None and old_age is not None and
                old_signing[0]!=key_id(public) and old_age[0]!=encryption_key_id(age_public),
                'upgrade_keys_must_change')
        signed={'subject_id':subject.resource_id,'handle':args['handle'],
                'public_key':args['public_key'],
                'encryption_recipient':args['encryption_recipient'],
                'request_id':request.request_id}
        verify(public,canonical(signed),decode(Signature,args['possession_proof']),
               purpose='custodial-upgrade-start')
        challenge_id=new_id('cupg')
        ephemeral=X25519PrivateKey.generate()
        expires=ctx.now+timedelta(seconds=300)
        challenge={'subject_id':subject.resource_id,'challenge_id':challenge_id,
                   'request_id':request.request_id,'handle':args['handle'],
                   'public_key':args['public_key'],
                   'encryption_recipient':args['encryption_recipient'],
                   'server_public':b64(ephemeral.public_key().public_bytes_raw()),
                   'nonce':b64(os.urandom(24)),'expires_at':wire(expires)}
        private_nonce,private_cipher=seal_upgrade_private(app,subject.resource_id,
            challenge_id,ephemeral.private_bytes_raw())
        vault_signature=open_signer(app,tx,subject.resource_id).sign(canonical(challenge),
            purpose='custodial-upgrade-challenge')
        body={'old_identity_key_id':old_signing[0],
              'old_encryption_key_id':old_age[0],
              'new_identity_key_id':key_id(public),
              'new_encryption_key_id':encryption_key_id(age_public),
              'age_inventory':await custodial_age_inventory(tx,subject.resource_id),
              'rewrap_mappings':{},'rewrap_acks':{},'inventory_version':1,
              'recovery_acks':{},
              'signing_possession_digest':digest(args['possession_proof']),
              'server_signature':wire(vault_signature)}
        tx.execute('''INSERT INTO custodial_upgrades
            (id,subject,credential_id,status,expires_at,challenge,ephemeral_nonce,
             ephemeral_ciphertext,body) VALUES (?,?,?,?,?,?,?,?,?)''',
            (challenge_id,subject.resource_id,ctx.principal.credential_id,'pending',
             wire(expires),canonical(challenge).decode(),private_nonce,private_cipher,
             canonical(body).decode()),write=True)
        return HandlerOutput(data={**challenge,'status':'pending',
                                   'signature_source':'custodial',
                                   'server_signature':wire(vault_signature)})

    @op('identity.custodial_upgrade_finish',obj({'challenge_id':IDENTIFIER,
        'age_proof':BYTES,'external_ciphertexts_migrated':BOOLEAN,
        'migration_ack':SIGNATURE},
        ('challenge_id','age_proof','external_ciphertexts_migrated','migration_ack')))
    @op('identity.custodial_upgrade_finish',obj({'challenge_id':IDENTIFIER,
        'age_proof':BYTES,'external_ciphertexts_migrated':BOOLEAN,
        'migration_ack':SIGNATURE,
        'action':{'enum':['refresh','recovery_envelope','finalize']},
        'inventory_digest':IDENTIFIER,'observed_digest':IDENTIFIER,'results_digest':IDENTIFIER,
        'resolution':{'enum':['verified','accept_loss','retain_decrypt']},
        'loss_revisions':{'type':'array','items':IDENTIFIER,'uniqueItems':True,'maxItems':10000},
        'reviewed_policy_version':{'type':'integer','minimum':0},
        'reason':{'type':'string','minLength':1,'maxLength':1000}},
        ('challenge_id','age_proof','external_ciphertexts_migrated','migration_ack')),version=2)
    async def custodial_upgrade_finish(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        args=request.arguments
        if 'action' in args:
            from msg.plugins.custodial_lifecycle import transition
            return await transition(app,ctx,request,tx,subject)
        require(subject.kind=='custodial' and ctx.principal.method=='token',
                'custodial_token_required')
        row=tx.one('''SELECT credential_id,status,expires_at,challenge,ephemeral_nonce,
            ephemeral_ciphertext,body FROM custodial_upgrades WHERE id=? AND subject=?''',
            (args['challenge_id'],subject.resource_id))
        require(row is not None and row[0]==ctx.principal.credential_id and row[1]=='pending',
                'custodial_upgrade_not_pending')
        require(ctx.now<parse_time(row[2]),'custodial_upgrade_expired')
        challenge=loads(row[3])
        details=loads(row[6])
        expected=server_upgrade_proof(app,subject.resource_id,challenge,row[4],row[5])
        if not hmac.compare_digest(expected,args['age_proof']):
            tx.execute("UPDATE custodial_upgrades SET status='failed',ephemeral_nonce=NULL,ephemeral_ciphertext=NULL WHERE id=?",
                       (args['challenge_id'],),write=True)
            return HandlerOutput(data={'challenge_id':args['challenge_id'],'status':'failed',
                                       'reason':'age_possession_failed'})
        public=unb64(challenge['public_key'],limit=32)
        acknowledgement={'subject_id':subject.resource_id,'challenge_id':args['challenge_id'],
                         'age_proof':args['age_proof'],
                         'external_ciphertexts_migrated':args['external_ciphertexts_migrated'],
                         'request_id':request.request_id}
        try:
            verify(public,canonical(acknowledgement),decode(Signature,args['migration_ack']),
                   purpose='custodial-upgrade-finish')
        except Failure as exc:
            if exc.code!='invalid_signature':
                raise
            tx.execute("UPDATE custodial_upgrades SET status='failed',ephemeral_nonce=NULL,ephemeral_ciphertext=NULL WHERE id=?",
                       (args['challenge_id'],),write=True)
            return HandlerOutput(data={'challenge_id':args['challenge_id'],'status':'failed',
                                       'reason':'migration_ack_failed'})
        inventory=await custodial_age_inventory(tx,subject.resource_id)
        frozen=details.get('age_inventory')
        require(frozen is not None,'custodial_inventory_required')
        require(inventory==frozen,'custodial_inventory_changed')
        tracked=len(inventory)
        policy_row=tx.one('SELECT body FROM recovery_policies WHERE subject=? ORDER BY version DESC LIMIT 1',
                          (subject.resource_id,))
        policy_opted_in=bool(policy_row and loads(policy_row[0]).get('opted_in'))
        if tracked or policy_opted_in or not args['external_ciphertexts_migrated']:
            from msg.plugins.custodial_lifecycle import activate_encryption_target
            activate_encryption_target(tx,subject.resource_id,challenge,details,ctx.now)
            details=dict(details,
                external_ciphertexts_migrated=args['external_ciphertexts_migrated'],
                migration_ack=wire(args['migration_ack']),
                age_possession_digest=digest(args['age_proof']))
            state={'status':'pending_rewrap','challenge_id':args['challenge_id'],
                   'server_tracked_age_revisions':tracked,
                   'age_inventory_digest':digest(frozen),
                   'age_inventory':frozen,
                   'recovery_policy_requires_review':policy_opted_in,
                   'external_migration':'owner_not_confirmed' if not args['external_ciphertexts_migrated']
                   else 'owner_declared_only',
                   'token_remains_active':True,'vault_remains_active':True}
            tx.execute('''UPDATE custodial_upgrades SET status='pending_rewrap',
                ephemeral_nonce=NULL,ephemeral_ciphertext=NULL,body=? WHERE id=?''',
                (canonical(details).decode(),args['challenge_id']),write=True)
            return HandlerOutput(data=state)
        from msg.plugins.custodial_lifecycle import switch_identity
        return await switch_identity(app,ctx,request,tx,subject,challenge,details)

    @op('identity.custodial_upgrade_result',obj({'challenge_id':IDENTIFIER},('challenge_id',)),
        effect='read',signature=True)
    async def custodial_upgrade_result(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        require(subject.kind=='registered' and ctx.principal.method=='signature',
                'self_custody_signature_required')
        row=tx.one('SELECT subject,status,body FROM custodial_upgrades WHERE id=?',
                   (request.arguments['challenge_id'],))
        require(row is not None and row[0]==subject.resource_id and row[1]=='completed',
                'custodial_upgrade_not_completed')
        completed=loads(row[2]).get('completed_result')
        require(completed is not None and completed['identity_key_id']==ctx.principal.credential_id,
                'custodial_upgrade_new_key_required')
        return HandlerOutput(data=completed)

    @op('identity.register',obj({'handle':STRING,'public_key':BYTES,'encryption_recipient':STRING},
                                 ('handle','public_key','encryption_recipient')),signature=True,version=2)
    async def register(ctx,request,tx):
        await app.online_issuer(tx)
        public=unb64(request.arguments['public_key'],limit=32)
        encryption_public=public_from_recipient(request.arguments['encryption_recipient'])
        require(public!=encryption_public,'encryption_key_must_be_independent')
        user=await make_user(app,tx,ctx,ctx.principal.subject,request.arguments['handle'],'registered')
        credential=Credential(id=key_id(public),subject_id=user.id,kind='signing_key',verifier=public,
            ceiling=app.primary_ceiling(),not_before=ctx.now,expires_at=None,revoked_at=None)
        await tx.save_credential(credential,0)
        tx.execute('INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
                   (credential.id,user.id,b64(public),wire(ctx.now),None,1),write=True)
        encryption_id=encryption_key_id(encryption_public)
        tx.execute('INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
                   (encryption_id,user.id,request.arguments['encryption_recipient'],
                    b64(encryption_public),wire(ctx.now),None,1),write=True)
        cert=await issue_online(app,tx,user.id,credential.id,ctx,request)
        return HandlerOutput(resources=(ResourceRef(id=user.id),),data={'subject_id':user.id,'key_id':credential.id,
            'encryption_key_id':encryption_id,'encryption_recipient':request.arguments['encryption_recipient'],
            'certificate_id':cert.resource_id,'handle':request.arguments['handle']})

    async def personal_write(ctx,request,tx, *, name,kind,parent):
        subject=await controlled_owner(app,ctx,request,tx)
        body=request.arguments['body']
        validate_personal_body(body,agents=kind=='agents',
                               limit=min(65536,app.settings.server.limits.max_request_bytes))
        content_proof={}
        if request.contract_version==2:
            content_proof={'content_signature':request.arguments['content_signature'],
                           'revision_id':request.arguments['revision_id']}
        existing=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',(parent,name))
        if existing is None:
            require('expected_revision' not in request.arguments,'personal_revision_not_found')
            resource=await create_resource(app,ctx,request,tx,parent=parent,type='file',name=name,
                                           body=body,media_type='text/markdown',mode=0o600,
                                           resource_id=request.arguments.get('resource_id'),**content_proof)
        else:
            resource=await tx.resource(existing[0])
            require(request.arguments.get('resource_id',resource.id)==resource.id,
                    'personal_resource_mismatch')
            require(resource.type=='file' and resource.owner==subject.resource_id and
                    resource.state=='active',
                    'personal_resource_conflict')
            require(request.arguments.get('expected_revision')==resource.revision,
                    'revision_conflict')
            await assert_generation(request,resource)
            resource=await revise_resource(app,ctx,request,tx,resource,body,'text/markdown',
                signature=content_proof.get('content_signature'),
                revision_id=content_proof.get('revision_id'))
        save_personal_proof(tx,request,resource,subject.resource_id,kind,ctx.now)
        return output_for(resource,signature_source='self-custody',
                          proof_purpose='revision' if request.contract_version==2 else 'request')

    personal_signature_fields={'resource_id':IDENTIFIER,'revision_id':IDENTIFIER,
        'content_signature':SIGNATURE,'content_created_at':STRING}

    @op('identity.personal_put',obj({'kind':{'enum':['soul','agents']},'body':STRING,
        'expected_revision':IDENTIFIER},('kind','body')),signature=True)
    @op('identity.personal_put',obj({'kind':{'enum':['soul','agents']},'body':STRING,
        'expected_revision':IDENTIFIER,**personal_signature_fields},
        ('kind','body',*personal_signature_fields)),signature=True,version=2)
    async def personal_put(ctx,request,tx):
        subject=ctx.principal.subject
        require(subject is not None,'authentication_required')
        name='SOUL.md' if request.arguments['kind']=='soul' else 'AGENTS.md'
        return await personal_write(ctx,request,tx,name=name,kind=request.arguments['kind'],parent=subject)

    @op('identity.soul_visibility',obj({'visibility':{'enum':['private','public']}},
                                       ('visibility',)),signature=True)
    async def soul_visibility(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        row=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',
                   (subject.resource_id,'SOUL.md'))
        require(row is not None,'personal_resource_not_found')
        resource=await tx.resource(row[0])
        await assert_generation(request,resource)
        mode=0o644 if request.arguments['visibility']=='public' else 0o600
        require(resource.mode!=mode,'visibility_unchanged')
        updated=replace(resource,mode=mode,generation=resource.generation+1,
                        modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated,visibility=request.arguments['visibility'])

    @op('identity.note_put',obj({'name':STRING,'body':STRING,'expected_revision':IDENTIFIER},
                                ('name','body')),signature=True)
    @op('identity.note_put',obj({'name':STRING,'body':STRING,'expected_revision':IDENTIFIER,
        **personal_signature_fields},('name','body',*personal_signature_fields)),
        signature=True,version=2)
    async def note_put(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        folder=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',
                      (subject.resource_id,'notes'))
        if folder is None:
            notes=await create_resource(app,ctx,request,tx,parent=subject.resource_id,
                                        type='topic',name='notes',mode=0o700)
            parent=notes.id
        else:
            notes=await tx.resource(folder[0])
            require(notes.type=='topic' and notes.owner==subject.resource_id and
                    notes.mode&0o077==0,'personal_resource_conflict')
            parent=notes.id
        return await personal_write(ctx,request,tx,name=request.arguments['name'],kind='note',parent=parent)

    @op('identity.note_list',obj(),effect='read')
    async def note_list(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        folder=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',
                      (subject.resource_id,'notes'))
        if folder is None:
            return HandlerOutput(data={'items':[]})
        rows=tx.rows("SELECT id,name,revision FROM resources WHERE parent=? AND state='active' ORDER BY name,id",
                     (folder[0],))
        return HandlerOutput(data={'items':[{'id':rid,'name':name,'revision':revision}
                                            for rid,name,revision in rows]})

    @op('identity.note_get',obj({'name':STRING},('name',)),effect='read')
    async def note_get(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        folder=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',
                      (subject.resource_id,'notes'))
        require(folder is not None,'note_not_found')
        row=tx.one("SELECT id,revision FROM resources WHERE parent=? AND name=? AND state='active'",
                   (folder[0],request.arguments['name']))
        require(row is not None,'note_not_found')
        revision=await tx.revision(ResourceRef(id=row[0],revision=row[1]))
        body=(await app.contents.read_bytes(revision.content)).decode('utf-8')
        return HandlerOutput(data={'id':row[0],'name':request.arguments['name'],
                                   'revision':revision.id,'content':body})

    async def owned_personal_item(ctx,request,tx,folder_name,name,type):
        subject=await controlled_owner(app,ctx,request,tx)
        validate_name(name)
        folder=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',
                      (subject.resource_id,folder_name))
        require(folder is not None,f'{type}_not_found')
        directory=await tx.resource(folder[0])
        require(directory.type=='topic' and directory.owner==subject.resource_id and
                directory.mode&0o077==0,'personal_resource_conflict')
        row=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',
                   (directory.id,name))
        require(row is not None,f'{type}_not_found')
        resource=await tx.resource(row[0])
        require(resource.type==type and resource.owner==subject.resource_id and
                resource.mode&0o077==0,'personal_resource_conflict')
        return resource

    async def personal_item_state(ctx,request,tx,folder_name,type,state):
        resource=await owned_personal_item(ctx,request,tx,folder_name,
                                           request.arguments['name'],type)
        require(request.arguments['expected_revision']==resource.revision,'revision_conflict')
        await assert_generation(request,resource)
        require(resource.state!=state,'state_unchanged')
        require(resource.state in {'active','archived'},'personal_resource_conflict')
        updated=replace(resource,state=state,generation=resource.generation+1,
                        modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated,state=state)

    personal_state_args=obj({'name':STRING,'expected_revision':IDENTIFIER},
                            ('name','expected_revision'))

    @op('identity.note_archive',personal_state_args,signature=True)
    async def note_archive(ctx,request,tx):
        return await personal_item_state(ctx,request,tx,'notes','file','archived')

    @op('identity.note_restore',personal_state_args,signature=True)
    async def note_restore(ctx,request,tx):
        return await personal_item_state(ctx,request,tx,'notes','file','active')

    todo_status={'enum':['pending','in_progress','done']}
    todo_priority={'enum':['low','neutral','high']}
    todo_put_args=obj({'name':STRING,'title':STRING,'description':STRING,
        'status':todo_status,'priority':todo_priority,'due_at':{'type':['string','null']},
        'related_resource':{'anyOf':[REF,{'type':'null'}]},
        'expected_revision':IDENTIFIER},('name','title'))

    todo_v2_args=obj({'name':STRING,'title':STRING,'description':STRING,
        'status':todo_status,'priority':todo_priority,'due_at':{'type':['string','null']},
        'related_resource':{'anyOf':[REF,{'type':'null'}]},
        'expected_revision':IDENTIFIER,**personal_signature_fields},
        ('name','title',*personal_signature_fields))

    @op('identity.todo_put',todo_put_args,signature=True)
    @op('identity.todo_put',todo_v2_args,signature=True,version=2)
    async def todo_put(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        args=request.arguments
        name=validate_name(args['name'])
        title=args['title']
        description=args.get('description','')
        require(1<=len(title)<=240 and len(description)<=8192,'todo_text_too_large')
        validate_personal_body(title+'\n'+description,limit=16384)
        due_at=args.get('due_at')
        if due_at is not None:
            due_at=wire(parse_time(due_at))
        related=args.get('related_resource')
        if related is not None:
            rid=await resolve(tx,related['id'])
            await check_access(app,ctx,request,tx,rid,'read')
            if related.get('revision') is not None:
                await tx.revision(ResourceRef(id=rid,revision=related['revision']))
            related={'id':rid,'revision':related.get('revision')}
        body=canonical({'title':title,'description':description,
            'status':args.get('status','pending'),'priority':args.get('priority','neutral'),
            'due_at':due_at,'related_resource':related}).decode('utf-8')
        proof={}
        if request.contract_version==2:
            proof={'content_signature':args['content_signature'],'revision_id':args['revision_id'],
                   'resource_id':args['resource_id']}
        folder=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',
                      (subject.resource_id,'todos'))
        if folder is None:
            require('expected_revision' not in args,'todo_revision_not_found')
            directory=await create_resource(app,ctx,request,tx,parent=subject.resource_id,
                                            type='topic',name='todos',mode=0o700)
        else:
            directory=await tx.resource(folder[0])
            require(directory.type=='topic' and directory.owner==subject.resource_id and
                    directory.mode&0o077==0,'personal_resource_conflict')
        row=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',(directory.id,name))
        if row is None:
            require('expected_revision' not in args,'todo_revision_not_found')
            resource=await create_resource(app,ctx,request,tx,parent=directory.id,
                type='todo',name=name,body=body,media_type='application/json',mode=0o600,
                resource_id=proof.get('resource_id'),content_signature=proof.get('content_signature'),
                revision_id=proof.get('revision_id'))
        else:
            resource=await tx.resource(row[0])
            require(resource.type=='todo' and resource.owner==subject.resource_id and
                    resource.state=='active' and resource.mode&0o077==0,
                    'personal_resource_conflict')
            if proof:
                require(proof['resource_id']==resource.id,'personal_resource_mismatch')
            require(args.get('expected_revision')==resource.revision,'revision_conflict')
            await assert_generation(request,resource)
            resource=await revise_resource(app,ctx,request,tx,resource,body,'application/json',
                signature=proof.get('content_signature'),revision_id=proof.get('revision_id'))
        save_personal_proof(tx,request,resource,subject.resource_id,'todo',ctx.now)
        return output_for(resource,**loads(body))

    @op('identity.todo_list',obj({'after_name':STRING,'limit':{'type':'integer',
        'minimum':1,'maximum':100}}),effect='read')
    async def todo_list(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        folder=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',
                      (subject.resource_id,'todos'))
        if folder is None:
            return HandlerOutput(data={'items':[],'next_after_name':None})
        directory=await tx.resource(folder[0])
        require(directory.type=='topic' and directory.owner==subject.resource_id and
                directory.mode&0o077==0,'personal_resource_conflict')
        limit=request.arguments.get('limit',50)
        rows=tx.rows("SELECT id,name,revision,generation FROM resources WHERE parent=? "
            "AND type='todo' AND state='active' AND name>? ORDER BY name LIMIT ?",
            (directory.id,request.arguments.get('after_name',''),limit+1))
        page=rows[:limit]
        items=[]
        for rid,name,revision_id,generation in page:
            revision=await tx.revision(ResourceRef(id=rid,revision=revision_id))
            details=loads((await app.contents.read_bytes(revision.content)).decode('utf-8'))
            items.append({'id':rid,'name':name,'revision':revision_id,
                'generation':generation,'title':details['title'],'status':details['status'],
                'priority':details['priority'],'due_at':details['due_at']})
        return HandlerOutput(data={'items':items,
            'next_after_name':page[-1][1] if len(rows)>limit else None})

    @op('identity.todo_get',obj({'name':STRING},('name',)),effect='read')
    async def todo_get(ctx,request,tx):
        resource=await owned_personal_item(ctx,request,tx,'todos',
                                           request.arguments['name'],'todo')
        require(resource.state=='active','todo_not_found')
        revision=await tx.revision(ResourceRef(id=resource.id,revision=resource.revision))
        body=loads((await app.contents.read_bytes(revision.content)).decode('utf-8'))
        return HandlerOutput(data={'id':resource.id,'name':resource.name,
            'revision':revision.id,'generation':resource.generation,**body})

    @op('identity.todo_archive',personal_state_args,signature=True)
    async def todo_archive(ctx,request,tx):
        return await personal_item_state(ctx,request,tx,'todos','todo','archived')

    @op('identity.todo_restore',personal_state_args,signature=True)
    async def todo_restore(ctx,request,tx):
        return await personal_item_state(ctx,request,tx,'todos','todo','active')

    legacy_action={'enum':['publish_final_message','archive_public_profile',
                           'handoff_information','preserve_account','impersonate',
                           'publish_secret','modify_ca']}
    legacy_refs={'type':'array','items':REF,'maxItems':16}
    legacy_ids={'type':'array','items':IDENTIFIER,'maxItems':16,'uniqueItems':True}

    @op('identity.legacy_put',obj({'visibility':{'enum':['private','public']},
        'final_message':{'type':'string','maxLength':8192},
        'preservation':{'enum':['keep','archive','unspecified']},
        'allowed_actions':{'type':'array','items':legacy_action,'maxItems':8,'uniqueItems':True},
        'forbidden_actions':{'type':'array','items':legacy_action,'maxItems':8,'uniqueItems':True},
        'envelope_ids':legacy_ids,'custodian_refs':legacy_ids,
        'checkpoint_refs':legacy_refs,'handoff_refs':legacy_refs,
        'expected_revision':IDENTIFIER},('visibility',)),signature=True)
    async def legacy_put(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        args=request.arguments
        require(any(name in args for name in ('final_message','preservation','allowed_actions',
            'forbidden_actions','envelope_ids','custodian_refs','checkpoint_refs','handoff_refs')),
            'empty_legacy_directive')
        message=args.get('final_message','')
        validate_personal_body(message,limit=min(8192,app.settings.server.limits.max_request_bytes))
        allowed=set(args.get('allowed_actions',()))
        forbidden=set(args.get('forbidden_actions',()))
        require(not allowed&forbidden,'legacy_action_conflict')
        require(not allowed&{'impersonate','publish_secret','modify_ca'},
                'legacy_action_forbidden')
        envelope_ids=[]
        for envelope_id in args.get('envelope_ids',()):
            row=tx.one('SELECT owner,body FROM recovery_envelopes WHERE id=?',(envelope_id,))
            require(row is not None and row[0]==subject.resource_id,'legacy_envelope_not_owned')
            envelope=loads(row[1])
            ciphertext=decode(ResourceRef,envelope['ciphertext_ref'])
            await check_access(app,ctx,request,tx,ciphertext.id,'read')
            await tx.revision(ciphertext)
            envelope_ids.append(envelope_id)
        custodian_refs=[]
        platform={item.id for item in app.settings.recovery_custodians}
        for custodian in args.get('custodian_refs',()):
            if custodian not in platform:
                require(custodian.startswith('u_'),'unknown_custodian_ref')
                await tx.subject(custodian)
            custodian_refs.append(custodian)
        async def own_refs(key):
            values=[]
            for raw in args.get(key,()):
                ref=decode(ResourceRef,raw)
                resource=await tx.resource(ref.id)
                require(resource.owner==subject.resource_id,'legacy_reference_not_owned')
                await check_access(app,ctx,request,tx,ref.id,'read')
                if ref.revision is not None:
                    await tx.revision(ref)
                values.append(wire(ref))
            return values
        checkpoint_refs=await own_refs('checkpoint_refs')
        handoff_refs=await own_refs('handoff_refs')
        public_body={'kind':'legacy_directive','owner_subject':subject.resource_id,
                     'final_message':message,'preservation':args.get('preservation','unspecified'),
                     'allowed_actions':sorted(allowed),'forbidden_actions':sorted(forbidden),
                     'declaration_only':True}
        previous=tx.one('SELECT resource_id FROM legacy_directives WHERE subject=?',
                        (subject.resource_id,))
        mode=0o444 if args['visibility']=='public' else 0o600
        if previous is None:
            require('expected_revision' not in args,'legacy_revision_not_found')
            resource=await create_resource(app,ctx,request,tx,parent='t_last_will',
                type='legacy_directive',name='will-'+subject.resource_id[-16:]+'.md',
                body=canonical(public_body),media_type='application/json',mode=mode)
            tx.execute('INSERT INTO legacy_directives VALUES (?,?,?)',
                       (subject.resource_id,resource.id,wire(ctx.now)),write=True)
            old_digest=None
        else:
            resource=await tx.resource(previous[0])
            require(resource.owner==subject.resource_id and resource.state=='active',
                    'legacy_directive_inactive')
            require(not (resource.mode==0o600 and mode==0o444),
                    'legacy_private_history_cannot_be_published')
            require(args.get('expected_revision')==resource.revision,'revision_conflict')
            await assert_generation(request,resource)
            old_digest=digest(resource.revision)
            resource=await revise_resource(app,ctx,request,tx,resource,canonical(public_body),
                                           'application/json')
            if resource.mode!=mode:
                resource=replace(resource,mode=mode,generation=resource.generation+1,
                    modified_at=ctx.now,modified_by=ctx.principal.actor)
                await tx.replace(resource,resource.generation-1)
        version={'subject_id':subject.resource_id,'resource_id':resource.id,
                 'revision_id':resource.revision,'envelope_ids':envelope_ids,
                 'custodian_refs':custodian_refs,'checkpoint_refs':checkpoint_refs,
                 'handoff_refs':handoff_refs,'visibility':args['visibility'],
                 'created_at':wire(ctx.now),'signature_source':'self-custody',
                 'proof_purpose':'request','request_signature':wire(request.proof.signature),
                 'signed_envelope':b64(signing_bytes(request))}
        tx.execute('INSERT INTO legacy_directive_versions VALUES (?,?,?,?)',
                   (resource.revision,resource.id,subject.resource_id,canonical(version).decode()),write=True)
        event=Event(id=new_id('audit'),type='identity.legacy.record',time=ctx.now,
            request_id=request.request_id,actor=ctx.principal.actor,subject=subject.resource_id,
            resources=(ResourceRef(id=resource.id,revision=resource.revision),),
            data={'declaration_only':True,'revision_id':resource.revision,
                  'visibility':args['visibility'],'request_digest':request.payload_digest})
        await tx.append_audit(AuditEvent(event=event,
            authority=(ResourceRef(id=subject.resource_id),),before_digest=old_digest,
            after_digest=digest(resource.revision),previous_digest=None,
            entry_digest='',result='recorded'))
        return output_for(resource,declaration_only=True,signature_source='self-custody',
                          proof_purpose='request')

    @op('identity.legacy_archive',obj(),signature=True)
    async def legacy_archive(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        row=tx.one('SELECT resource_id FROM legacy_directives WHERE subject=?',
                   (subject.resource_id,))
        require(row is not None,'legacy_directive_not_found')
        resource=await tx.resource(row[0])
        require(resource.state=='active','legacy_directive_inactive')
        await assert_generation(request,resource)
        updated=replace(resource,state='archived',mode=0o600,
            generation=resource.generation+1,
            modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        event=Event(id=new_id('audit'),type='identity.legacy.archive',time=ctx.now,
            request_id=request.request_id,actor=ctx.principal.actor,subject=subject.resource_id,
            resources=(ResourceRef(id=resource.id,revision=resource.revision),),
            data={'declaration_only':True,'revision_id':resource.revision})
        await tx.append_audit(AuditEvent(event=event,
            authority=(ResourceRef(id=subject.resource_id),),before_digest=digest(resource),
            after_digest=digest(updated),previous_digest=None,entry_digest='',result='archived'))
        return output_for(updated,state='archived',declaration_only=True)

    @op('identity.legacy_get',obj({'subject_id':IDENTIFIER,'revision':IDENTIFIER},
                                  ('subject_id',)),effect='read')
    async def legacy_get(ctx,request,tx):
        subject_id=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(subject_id)
        row=tx.one('SELECT resource_id FROM legacy_directives WHERE subject=?',(subject_id,))
        require(row is not None,'legacy_directive_not_found')
        resource=await tx.resource(row[0])
        require(resource.state=='active' or ctx.principal.subject==subject_id,
                'legacy_directive_archived')
        await check_access(app,ctx,request,tx,resource.id,'read')
        revision=await tx.revision(ResourceRef(id=resource.id,
                                              revision=request.arguments.get('revision')))
        body=loads(await app.contents.read_bytes(revision.content))
        metadata=tx.one('SELECT body FROM legacy_directive_versions WHERE revision_id=?',
                        (revision.id,))
        require(metadata is not None,'legacy_version_not_found')
        details=loads(metadata[0])
        require(details['visibility']=='public' or ctx.principal.subject==subject_id,
                'legacy_directive_private')
        output=dict(body,id=resource.id,revision=revision.id,
                    state=resource.state,visibility=details['visibility'],
                    envelope_ids=[],custodian_refs=[],checkpoint_refs=[],handoff_refs=[])
        if ctx.principal.subject==subject_id:
            for envelope_id in details['envelope_ids']:
                envelope=tx.one('SELECT body FROM recovery_envelopes WHERE id=?',(envelope_id,))
                if envelope is None:
                    continue
                ref=decode(ResourceRef,loads(envelope[0])['ciphertext_ref'])
                try:
                    await check_access(app,ctx,request,tx,ref.id,'read')
                    await tx.revision(ref)
                except Failure:
                    continue
                output['envelope_ids'].append(envelope_id)
            for key in ('checkpoint_refs','handoff_refs'):
                for raw in details[key]:
                    ref=decode(ResourceRef,raw)
                    try:
                        await check_access(app,ctx,request,tx,ref.id,'read')
                        if ref.revision is not None:
                            await tx.revision(ref)
                    except Failure:
                        continue
                    output[key].append(raw)
            output['custodian_refs']=list(details['custodian_refs'])
            output['signature_source']=details['signature_source']
            output['proof_purpose']=details['proof_purpose']
            output['request_signature']=details['request_signature']
            output['signed_envelope']=details['signed_envelope']
        return HandlerOutput(data=output)

    @op('identity.legacy_status',obj(),effect='read')
    async def legacy_status(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        row=tx.one('SELECT state FROM legacy_states WHERE subject=?',(subject.resource_id,))
        return HandlerOutput(data={'subject_id':subject.resource_id,
                                   'state':row[0] if row else 'active',
                                   'automatic_transition':False})

    @op('identity.identity_key_list',obj({'subject_id':IDENTIFIER},('subject_id',)),effect='read')
    async def identity_key_list(ctx,request,tx):
        subject=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(subject)
        rows=tx.rows('''SELECT key_id,public_key,created_at,retired_at,is_primary
            FROM identity_keys WHERE subject=? ORDER BY created_at,key_id''',(subject,))
        return HandlerOutput(data={'subject_id':subject,'keys':[
            {'key_id':key,'public_key':public,'created_at':created,'retired_at':retired,
             'primary':bool(primary),'algorithm':'ed25519','purpose':'identity'}
            for key,public,created,retired,primary in rows]})

    @op('identity.identity_key_get',obj({'subject_id':IDENTIFIER,'key_id':IDENTIFIER},('subject_id',)),effect='read')
    async def identity_key_get(ctx,request,tx):
        subject=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(subject)
        key=request.arguments.get('key_id')
        row=tx.one('''SELECT key_id,public_key,created_at,retired_at,is_primary
            FROM identity_keys WHERE subject=? AND '''+('key_id=?' if key else 'is_primary=1'),
            (subject,key) if key else (subject,))
        require(row is not None,'identity_key_not_found')
        key,public,created,retired,primary=row
        return HandlerOutput(data={'subject_id':subject,'key_id':key,'public_key':public,
            'created_at':created,'retired_at':retired,'primary':bool(primary),
            'algorithm':'ed25519','purpose':'identity'})

    @op('identity.encryption_key_list',obj({'subject_id':IDENTIFIER},('subject_id',)),effect='read')
    async def encryption_key_list(ctx,request,tx):
        subject=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(subject)
        rows=tx.rows('''SELECT key_id,recipient,public_key,created_at,retired_at,is_primary
            FROM encryption_subkeys WHERE subject=? ORDER BY created_at,key_id''',(subject,))
        return HandlerOutput(data={'subject_id':subject,'keys':[
            {'key_id':key,'recipient':recipient,'public_key':public,'created_at':created,
             'retired_at':retired,'primary':bool(primary),'algorithm':'age-x25519',
             'purpose':'encryption'} for key,recipient,public,created,retired,primary in rows]})

    @op('identity.encryption_key_get',obj({'subject_id':IDENTIFIER,'key_id':IDENTIFIER},('subject_id',)),effect='read')
    async def encryption_key_get(ctx,request,tx):
        subject=await resolve(tx,request.arguments['subject_id'])
        await tx.subject(subject)
        key=request.arguments.get('key_id')
        row=tx.one('''SELECT key_id,recipient,public_key,created_at,retired_at,is_primary
            FROM encryption_subkeys WHERE subject=? AND '''+('key_id=?' if key else 'is_primary=1'),
            (subject,key) if key else (subject,))
        require(row is not None,'encryption_key_not_found')
        key,recipient,public,created,retired,primary=row
        return HandlerOutput(data={'subject_id':subject,'key_id':key,'recipient':recipient,
            'public_key':public,'created_at':created,'retired_at':retired,'primary':bool(primary),
            'algorithm':'age-x25519','purpose':'encryption'})

    @op('identity.encryption_key_rotate',obj({'encryption_recipient':STRING},
                                              ('encryption_recipient',)),signature=True)
    async def encryption_key_rotate(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        require(tx.one("""SELECT 1 FROM custodial_upgrades u LEFT JOIN custodial_vault v
            ON v.subject=u.subject WHERE u.subject=? AND (u.status='pending_rewrap'
            OR (u.status='pending' AND u.expires_at>?) OR v.status='decrypt_only') LIMIT 1""",
            (subject.resource_id,wire(ctx.now))) is None,'custodial_migration_pending')
        public=public_from_recipient(request.arguments['encryption_recipient'])
        new_key=encryption_key_id(public)
        current=tx.one('SELECT key_id FROM encryption_subkeys WHERE subject=? AND is_primary=1',
                       (subject.resource_id,))
        require(current is not None,'encryption_key_not_found')
        require(current[0]!=new_key,'encryption_key_unchanged')
        require(tx.one('SELECT 1 FROM encryption_subkeys WHERE key_id=?',(new_key,)) is None,
                'encryption_key_exists')
        tx.execute('''UPDATE encryption_subkeys SET is_primary=0,retired_at=?
            WHERE subject=? AND is_primary=1''',(wire(ctx.now),subject.resource_id),write=True)
        tx.execute('INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
                   (new_key,subject.resource_id,request.arguments['encryption_recipient'],
                    b64(public),wire(ctx.now),None,1),write=True)
        return HandlerOutput(data={'subject_id':subject.resource_id,'key_id':new_key,
            'recipient':request.arguments['encryption_recipient'],'previous_key_id':current[0],
            'old_ciphertexts_require_rewrap':True})

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
        remaining=int((source.expires_at-ctx.now).total_seconds())
        require(remaining>=1,'renewal_source_expiring')
        ttl=request.arguments.get('ttl',remaining)
        require(ttl<=remaining,'renewal_ttl_exceeded')
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
    @op('identity.temporary',obj({'nonce':BYTES,'recovery_secret':BYTES},
                                 ('nonce','recovery_secret')),version=2)
    @op('identity.temporary',obj({'nonce':BYTES,'recovery_secret':BYTES,
        'public_key':BYTES,'encryption_recipient':STRING,
        'possession_proof':SIGNATURE},
        ('nonce','recovery_secret','public_key','encryption_recipient',
         'possession_proof')),version=3)
    async def temporary(ctx,request,tx):
        if request.contract_version<3:
            raise Failure('temporary_dual_keys_required',
                          details={'contract_version':3})
        token=app.issued_token(request,ctx.principal.subject)
        public=encryption_public=None
        if request.contract_version==3:
            public=unb64(request.arguments['public_key'],limit=32)
            encryption_public=public_from_recipient(request.arguments['encryption_recipient'])
            require(public!=encryption_public,'encryption_key_must_be_independent')
            verify(public,canonical({'subject_id':ctx.principal.subject,
                'nonce':request.arguments['nonce'],
                'public_key':request.arguments['public_key'],
                'encryption_recipient':request.arguments['encryption_recipient'],
                'request_id':request.request_id}),
                decode(Signature,request.arguments['possession_proof']),
                purpose='temporary-key-possession-v1')
        user=await make_user(app,tx,ctx,ctx.principal.subject,'tmp-'+ctx.principal.subject[-24:],'temporary')
        if public is not None:
            signing_id=key_id(public)
            signing_credential=Credential(id=signing_id,subject_id=user.id,
                kind='signing_key',verifier=public,ceiling=app.temporary_ceiling(),not_before=ctx.now,
                expires_at=None,revoked_at=None)
            await tx.save_credential(signing_credential,0)
            tx.execute('INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
                       (signing_id,user.id,b64(public),wire(ctx.now),None,1),write=True)
            encryption_id=encryption_key_id(encryption_public)
            tx.execute('INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
                       (encryption_id,user.id,request.arguments['encryption_recipient'],
                        b64(encryption_public),wire(ctx.now),None,1),write=True)
        credential=Credential(id=ctx.principal.credential_id,subject_id=user.id,kind='token',
            verifier=hashlib.sha256(token).digest(),ceiling=app.temporary_ceiling(),not_before=ctx.now,
            expires_at=ctx.now+timedelta(seconds=app.settings.temporary_ttl),revoked_at=None)
        await tx.save_credential(credential,0)
        app.record_token_delivery(tx,request,credential,ctx.now)
        result={'subject_id':user.id,'credential_id':credential.id,
                'expires_at':wire(credential.expires_at)}
        if public is not None:
            result.update(identity_key_id=signing_id,
                          encryption_key_id=encryption_id,
                          encryption_recipient=request.arguments['encryption_recipient'],
                          signature_source='self_custody',server_signable=False,
                          server_decryptable=False)
        return HandlerOutput(resources=(ResourceRef(id=user.id),),data=result)

    @op('identity.token_rotate',obj({'nonce':BYTES},('nonce',)))
    @op('identity.token_rotate',obj({'nonce':BYTES,'recovery_secret':BYTES},
                                    ('nonce','recovery_secret')),version=2)
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
        app.record_token_delivery(tx,request,credential,ctx.now)
        await tx.save_credential(replace(old,revoked_at=ctx.now),subject.auth_version)
        return HandlerOutput(data={'subject_id':subject.resource_id,'credential_id':credential.id,
            'previous_credential':old.id,'expires_at':wire(credential.expires_at)})

    @op('identity.upgrade',obj({'handle':STRING,'public_key':BYTES,'possession_proof':SIGNATURE},('handle','public_key','possession_proof')))
    async def upgrade_legacy(ctx,request,tx):
        raise Failure('encryption_subkey_required',details={'contract_version':2})

    @op('identity.upgrade',obj({'handle':STRING,'public_key':BYTES,'possession_proof':SIGNATURE,
                                'encryption_recipient':STRING},
                               ('handle','public_key','possession_proof','encryption_recipient')),version=2)
    async def upgrade(ctx,request,tx):
        await app.online_issuer(tx)
        subject=await controlled_owner(app,ctx,request,tx)
        require(subject.kind=='temporary','not_temporary')
        a=request.arguments
        public=unb64(a['public_key'],limit=32)
        encryption_public=public_from_recipient(a['encryption_recipient'])
        require(public!=encryption_public,'encryption_key_must_be_independent')
        verify(public,canonical({'subject_id':subject.resource_id,'public_key':a['public_key'],'handle':a['handle'],
                                 'encryption_recipient':a['encryption_recipient']}),
               decode(Signature,a['possession_proof']),purpose='upgrade')
        require(re.fullmatch(r'[a-z][a-z0-9-]{1,40}',a['handle']) is not None and a['handle'] not in {'root','online-ca'},'invalid_handle')
        resource=await tx.resource(subject.resource_id)
        updated=replace(resource,name='@'+a['handle'],generation=resource.generation+1,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        await tx.update_identity(replace(subject,kind='registered',auth_version=subject.auth_version+1),subject.auth_version)
        credential=Credential(id=key_id(public),subject_id=subject.resource_id,kind='signing_key',verifier=public,
            ceiling=app.primary_ceiling(),not_before=ctx.now,expires_at=None,revoked_at=None)
        await tx.save_credential(credential,subject.auth_version+1)
        existing_key=tx.one('SELECT key_id,public_key FROM identity_keys WHERE subject=? AND is_primary=1',
                            (subject.resource_id,))
        if existing_key is None:
            tx.execute('INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
                       (credential.id,subject.resource_id,b64(public),wire(ctx.now),None,1),write=True)
        else:
            require(existing_key==(credential.id,b64(public)),
                    'temporary_identity_key_mismatch')
        encryption_id=encryption_key_id(encryption_public)
        existing_encryption=tx.one('''SELECT key_id,recipient FROM encryption_subkeys
            WHERE subject=? AND is_primary=1''',(subject.resource_id,))
        if existing_encryption is None:
            tx.execute('INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
                       (encryption_id,subject.resource_id,a['encryption_recipient'],
                        b64(encryption_public),wire(ctx.now),None,1),write=True)
        else:
            require(existing_encryption==(encryption_id,a['encryption_recipient']),
                    'temporary_encryption_key_mismatch')
        certificate=await issue_online(app,tx,subject.resource_id,credential.id,ctx,request)
        for (raw,) in tx.rows('SELECT body FROM credentials WHERE subject=?',
                              (subject.resource_id,)):
            previous=decode(Credential,loads(raw))
            if previous.kind=='token' and previous.revoked_at is None:
                await tx.save_credential(replace(previous,revoked_at=ctx.now),
                                         subject.auth_version+1)
        return HandlerOutput(resources=(ResourceRef(id=subject.resource_id),),data={'subject_id':subject.resource_id,
            'key_id':credential.id,'encryption_key_id':encryption_id,
            'encryption_recipient':a['encryption_recipient'],
            'certificate_id':certificate.resource_id,'handle':a['handle']})

    @op('identity.upgrade_result',obj({'upgrade_request_id':IDENTIFIER},('upgrade_request_id',)),
        effect='read',signature=True)
    async def upgrade_result(ctx,request,tx):
        # The request ID locates a committed fact; it never authenticates a caller.
        # Ordinary authentication checks current key revocation and its ceiling
        # before this read-only handler can look up any prior result.
        subject=await controlled_owner(app,ctx,request,tx)
        require(subject.kind=='registered' and ctx.principal.method=='signature',
                'self_custody_signature_required')
        row=tx.one('SELECT body FROM results WHERE subject=? AND request_id=?',
                   (subject.resource_id,request.arguments['upgrade_request_id']))
        require(row is not None,'upgrade_not_completed')
        completed=loads(row[0])
        require(completed.get('operation')=='identity.upgrade' and
                completed.get('status')=='ok' and completed.get('subject')==subject.resource_id,
                'upgrade_not_completed')
        data=completed['data']
        require(data['key_id']==ctx.principal.credential_id,'upgrade_new_key_required')
        primary=tx.one('SELECT key_id FROM identity_keys WHERE subject=? AND is_primary=1 AND retired_at IS NULL',
                       (subject.resource_id,))
        require(primary==(data['key_id'],),'upgrade_new_key_required')
        encryption=tx.one('SELECT subject,recipient,retired_at FROM encryption_subkeys WHERE key_id=?',
                          (data['encryption_key_id'],))
        require(encryption==(subject.resource_id,data['encryption_recipient'],None),
                'upgrade_encryption_key_changed')
        certificate=await app.certificates.validate(data['certificate_id'],tx)
        require(certificate.subject_id==subject.resource_id and certificate.key_id==data['key_id'],
                'upgrade_certificate_mismatch')
        return HandlerOutput(data={**{field:data[field] for field in (
            'subject_id','key_id','encryption_key_id','encryption_recipient','certificate_id','handle')},
            'status':'completed','upgrade_request_id':request.arguments['upgrade_request_id'],
            'upgrade_committed_at':completed['committed_at']})

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
        tx.execute('INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
                   (credential.id,subject.resource_id,b64(public),wire(ctx.now),None,0),write=True)
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
        current=tx.one('SELECT is_primary FROM identity_keys WHERE key_id=? AND subject=?',
                       (credential.id,subject.resource_id))
        if current is not None:
            tx.execute('UPDATE identity_keys SET is_primary=0,retired_at=? WHERE key_id=?',
                       (wire(ctx.now),credential.id),write=True)
            if current[0]:
                replacement=tx.one('''SELECT key_id FROM identity_keys WHERE subject=? AND retired_at IS NULL
                    ORDER BY created_at,key_id LIMIT 1''',(subject.resource_id,))
                if replacement is not None:
                    tx.execute('UPDATE identity_keys SET is_primary=1 WHERE key_id=?',
                               (replacement[0],),write=True)
        await tx.update_identity(replace(subject,auth_version=subject.auth_version+1),subject.auth_version)
        return HandlerOutput(data={'key_id':credential.id,'revoked':True})

    @op('identity.token_create',obj({'nonce':BYTES,'ceiling':GRANTS,'ttl':{'type':'integer','minimum':1,'maximum':86400}},
                                    ('nonce','ceiling','ttl')),signature=True)
    @op('identity.token_create',obj({'nonce':BYTES,'ceiling':GRANTS,'ttl':{'type':'integer','minimum':1,'maximum':86400},
                                     'recovery_secret':BYTES},('nonce','ceiling','ttl','recovery_secret')),
        signature=True,version=2)
    async def token_create(ctx,request,tx):
        subject=await controlled_owner(app,ctx,request,tx)
        ceiling=tuple(decode(CapabilityGrant,g) for g in request.arguments['ceiling'])
        await validate_ceiling(app,ctx,tx,ceiling)
        require(len(unb64(request.arguments['nonce']))>=24,'invalid_bootstrap_nonce')
        credential=Credential(id='t_'+digest((request.request_id,subject.resource_id))[7:39],subject_id=subject.resource_id,
            kind='token',verifier=hashlib.sha256(app.issued_token(request,subject.resource_id)).digest(),ceiling=ceiling,
            not_before=ctx.now,expires_at=ctx.now+timedelta(seconds=request.arguments['ttl']),revoked_at=None)
        await tx.save_credential(credential,subject.auth_version)
        app.record_token_delivery(tx,request,credential,ctx.now)
        return HandlerOutput(data={'subject_id':subject.resource_id,'credential_id':credential.id,'expires_at':wire(credential.expires_at)})

    @op('identity.token_recover',obj({
        'credential_id':STRING,'original_request_id':STRING,
        'recovery_secret':BYTES,'nonce':BYTES,
        'new_recovery_secret':BYTES},
        ('credential_id','original_request_id','recovery_secret','nonce','new_recovery_secret')))
    async def token_recover(ctx,request,tx):
        # Authentication checked the pre-bound recovery verifier in this same
        # transaction. The one-use state and revocation commit with the result.
        subject=await controlled_owner(app,ctx,request,tx)
        old=await tx.credential(request.arguments['credential_id'])
        require(old.subject_id==subject.resource_id and old.kind=='token' and
                old.revoked_at is None and old.expires_at>ctx.now,'recovery_unavailable')
        row=tx.one('''SELECT request_id,consumed_at,recovery_expires_at FROM token_deliveries
            WHERE credential_id=? AND subject=?''',(old.id,subject.resource_id))
        require(row is not None and row[0]==request.arguments['original_request_id']
                and row[1] is None and parse_time(row[2])>ctx.now
                and request.request_id!=row[0],'recovery_unavailable')
        require(len(unb64(request.arguments['nonce'],limit=64))>=24,'invalid_bootstrap_nonce')
        new_id='t_'+digest((request.request_id,subject.resource_id))[7:39]
        require(new_id!=old.id,'recovery_unavailable')
        token=app.issued_token(request,subject.resource_id)
        credential=Credential(id=new_id,subject_id=subject.resource_id,kind='token',
            verifier=hashlib.sha256(token).digest(),ceiling=old.ceiling,
            not_before=ctx.now,expires_at=old.expires_at,revoked_at=None)
        app.record_token_delivery(tx,request,credential,ctx.now,
                                  recovery_deadline=parse_time(row[2]))
        await tx.save_credential(credential,subject.auth_version)
        await tx.save_credential(replace(old,revoked_at=ctx.now),subject.auth_version)
        consumed=tx.execute('''UPDATE token_deliveries SET consumed_at=? WHERE credential_id=?
            AND consumed_at IS NULL''',(wire(ctx.now),old.id),write=True)
        require(consumed.rowcount==1,'recovery_unavailable')
        return HandlerOutput(data={'subject_id':subject.resource_id,
            'credential_id':credential.id,'previous_credential':old.id,
            'expires_at':wire(credential.expires_at)})

    @op('group.create',obj({'name':STRING},('name',)),signature=True)
    async def group_create(ctx,request,tx):
        await app.authorizer.require_base(ctx.principal,operation_id(request),ctx.principal.subject,tx)
        name=request.arguments['name']
        require(re.fullmatch(r'[a-z][a-z0-9-]{1,40}',name) is not None and name not in {'public','admins'},'invalid_group_name')
        rid=new_id('g')
        r=Resource(id=rid,type='organization',type_version=1,name='&'+name,parent=app.namespace_root,
            owner=ctx.principal.subject,group=rid,mode=0o2775,generation=0,revision=None,state='active',
            created_at=ctx.now,created_by=ctx.principal.actor,modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.insert(r)
        await tx.update_identity(Organization(resource_id=rid,membership_version=0),-1)
        await set_member(tx,rid,ctx.principal.subject,'owner',joined_at=ctx.now)
        return output_for(r)

    async def group_context(ctx,request,tx):
        org=await resolve(tx,request.arguments['group'])
        resource=await tx.resource(org)
        require(resource.type=='organization','group_not_found')
        organization=await tx.organization(org)
        require(organization.builtin!='public','protected_group')
        member=group_member(tx,org,ctx.principal.subject)
        role=('owner' if resource.owner==ctx.principal.subject else
              'maintainer' if member and member.status=='active' and member.role in {'admin','maintainer'} else
              'member' if member and member.status=='active' else None)
        return org,resource,organization,member,role

    async def group_manager(ctx,request,tx,org,role, *, owner=False):
        operation=operation_id(request)
        await app.authorizer._ceiling(ctx.principal,operation,org,tx)
        ordinary=await app.authorizer.ordinary(ctx.principal,operation,org,tx)
        override=await app.authorizer.has(ctx.principal,'group.manage_override',operation,org,tx)
        require((ordinary and (role=='owner' if owner else role in {'owner','maintainer'})) or
                (override and not owner),'group_admin_required')
        return role=='owner' or override

    async def group_change(ctx,request,tx):
        org,resource,organization,_,role=await group_context(ctx,request,tx)
        manager_is_owner=await group_manager(ctx,request,tx,org,role)
        target=await resolve(tx,request.arguments['subject'])
        await tx.subject(target)
        action=request.operation
        previous=group_member(tx,org,target)
        require(target!=resource.owner,'cannot_remove_group_owner')
        if previous and previous.status=='active' and previous.role in {'owner','maintainer','admin'}:
            require(manager_is_owner,'group_owner_required')
        if action=='group.member.remove':
            await remove_group_member(tx,org,target)
        else:
            if action=='group.admin.add':
                require(manager_is_owner,'group_owner_required')
            if action=='group.admin.remove':
                require(previous is not None and previous.status=='active'
                        and previous.role in {'admin','maintainer'},'group_membership_not_active')
            stored=tx.one("SELECT body FROM identities WHERE id=? AND kind='organization'",(org,))
            legacy_policy=stored is not None and 'membership_policy' not in loads(stored[0])
            needs_acceptance=(organization.builtin is None and not legacy_policy and
                              organization.membership_policy in {'invite','approval'} and
                              (previous is None or previous.status!='active'))
            if action=='group.admin.add':
                require(not needs_acceptance,'group_membership_not_active')
            status='invited' if action=='group.member.add' and needs_acceptance else 'active'
            await set_member(tx,org,target,'maintainer' if action=='group.admin.add' else 'member',
                             status=status,
                             joined_at=(previous.joined_at if previous and previous.joined_at else ctx.now)
                             if status=='active' else None,
                             invited_by=previous.invited_by if previous else ctx.principal.subject)
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'subject_id':target,'action':action,
            'status':'removed' if action=='group.member.remove' else status})
    for name in ('group.member.add','group.member.remove','group.admin.add','group.admin.remove'):
        op(name,obj({'group':IDENTIFIER,'subject':IDENTIFIER},('group','subject')),signature=True)(group_change)

    @op('group.policy_get',obj({'group':IDENTIFIER},('group',)),effect='read')
    async def group_policy_get(ctx,request,tx):
        org,_,organization,_,_=await group_context(ctx,request,tx)
        await check_access(app,ctx,request,tx,org,'read')
        return HandlerOutput(resources=(ResourceRef(id=org),),data={
            'membership_policy':organization.membership_policy,
            'membership_version':organization.membership_version})

    @op('group.members',obj({'group':IDENTIFIER},('group',)),effect='read',signature=True)
    async def group_members(ctx,request,tx):
        org,resource,_,_,role=await group_context(ctx,request,tx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),org,tx)
        require(role is not None,'group_membership_required')
        rows=tx.rows('SELECT body FROM memberships WHERE org=? ORDER BY subject',(org,))
        members=[wire(decode(Membership,loads(row[0]))) for row in rows]
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'members':members,
            'owner':resource.owner})

    @op('group.policy_set',obj({'group':IDENTIFIER,'policy':{'enum':['open','approval','invite','managed']}},
                               ('group','policy')),signature=True)
    async def group_policy_set(ctx,request,tx):
        org,_,organization,_,role=await group_context(ctx,request,tx)
        await group_manager(ctx,request,tx,org,role,owner=True)
        policy=request.arguments['policy']
        if policy!=organization.membership_policy:
            await tx.update_identity(replace(organization,membership_policy=policy,
                                             membership_version=organization.membership_version+1),
                                     organization.membership_version)
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'membership_policy':policy})

    @op('group.join',obj({'group':IDENTIFIER},('group',)),signature=True)
    async def group_join(ctx,request,tx):
        org,_,organization,member,_=await group_context(ctx,request,tx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),org,tx)
        require(organization.membership_policy in {'open','approval','invite'},'group_managed')
        require(member is None or member.status!='active','already_group_member')
        if organization.membership_policy=='invite':
            require(member is not None and member.status=='invited','group_invitation_required')
        status=('pending' if organization.membership_policy=='approval' and
                (member is None or member.status!='invited') else 'active')
        await set_member(tx,org,ctx.principal.subject,'member',status=status,
                         joined_at=ctx.now if status=='active' else None,
                         invited_by=member.invited_by if member else None)
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'status':status})

    @op('group.invite',obj({'group':IDENTIFIER,'subject':IDENTIFIER},('group','subject')),signature=True)
    async def group_invite(ctx,request,tx):
        org,_,_,_,role=await group_context(ctx,request,tx)
        await group_manager(ctx,request,tx,org,role)
        target=await resolve(tx,request.arguments['subject'])
        subject=await tx.subject(target)
        require(subject.kind in {'registered','custodial'} and not subject.local_only,'invalid_group_subject')
        previous=group_member(tx,org,target)
        require(previous is None or previous.status!='active','already_group_member')
        await set_member(tx,org,target,'member',status='invited',invited_by=ctx.principal.subject)
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'subject_id':target,'status':'invited'})

    @op('group.approve',obj({'group':IDENTIFIER,'subject':IDENTIFIER},('group','subject')),signature=True)
    async def group_approve(ctx,request,tx):
        org,_,_,_,role=await group_context(ctx,request,tx)
        await group_manager(ctx,request,tx,org,role)
        target=await resolve(tx,request.arguments['subject'])
        previous=group_member(tx,org,target)
        require(previous is not None and previous.status=='pending','group_request_not_pending')
        await set_member(tx,org,target,'member',joined_at=ctx.now,invited_by=ctx.principal.subject)
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'subject_id':target,'status':'active'})

    @op('group.reject',obj({'group':IDENTIFIER,'subject':IDENTIFIER},('group','subject')),signature=True)
    async def group_reject(ctx,request,tx):
        org,_,_,_,role=await group_context(ctx,request,tx)
        await group_manager(ctx,request,tx,org,role)
        target=await resolve(tx,request.arguments['subject'])
        previous=group_member(tx,org,target)
        require(previous is not None and previous.status=='pending','group_request_not_pending')
        await set_member(tx,org,target,'member',status='rejected')
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'subject_id':target,'status':'rejected'})

    @op('group.leave',obj({'group':IDENTIFIER},('group',)),signature=True)
    async def group_leave(ctx,request,tx):
        org,resource,_,member,_=await group_context(ctx,request,tx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),org,tx)
        require(ctx.principal.subject!=resource.owner,'cannot_remove_group_owner')
        require(member is not None,'group_membership_not_found')
        await remove_group_member(tx,org,ctx.principal.subject)
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'status':'left'})

    @op('group.remove',obj({'group':IDENTIFIER,'subject':IDENTIFIER},('group','subject')),signature=True)
    async def group_remove(ctx,request,tx):
        org,resource,_,_,role=await group_context(ctx,request,tx)
        manager_is_owner=await group_manager(ctx,request,tx,org,role)
        target=await resolve(tx,request.arguments['subject'])
        require(target!=resource.owner,'cannot_remove_group_owner')
        member=group_member(tx,org,target)
        require(member is not None,'group_membership_not_found')
        if member.status=='active' and member.role in {'owner','maintainer','admin'}:
            require(manager_is_owner,'group_owner_required')
        await remove_group_member(tx,org,target)
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'subject_id':target,'status':'removed'})

    @op('group.role_set',obj({'group':IDENTIFIER,'subject':IDENTIFIER,
                              'role':{'enum':['member','maintainer']}},('group','subject','role')),signature=True)
    async def group_role_set(ctx,request,tx):
        org,resource,_,_,role=await group_context(ctx,request,tx)
        await group_manager(ctx,request,tx,org,role,owner=True)
        target=await resolve(tx,request.arguments['subject'])
        require(target!=resource.owner,'cannot_demote_group_owner')
        member=group_member(tx,org,target)
        require(member is not None and member.status=='active','group_membership_not_active')
        await set_member(tx,org,target,request.arguments['role'],joined_at=member.joined_at,
                         invited_by=member.invited_by)
        return HandlerOutput(resources=(ResourceRef(id=org),),data={'subject_id':target,'role':request.arguments['role']})

    @op('group.owner_transfer',obj({'group':IDENTIFIER,'subject':IDENTIFIER},('group','subject')),signature=True)
    async def group_owner_transfer(ctx,request,tx):
        org,resource,_,current,role=await group_context(ctx,request,tx)
        await group_manager(ctx,request,tx,org,role,owner=True)
        target=await resolve(tx,request.arguments['subject'])
        require(target!=resource.owner,'already_group_owner')
        member=group_member(tx,org,target)
        require(member is not None and member.status=='active','group_membership_not_active')
        require(current is not None and current.status=='active','group_owner_missing')
        await set_member(tx,org,resource.owner,'maintainer',joined_at=current.joined_at,
                         invited_by=current.invited_by)
        await set_member(tx,org,target,'owner',joined_at=member.joined_at,invited_by=member.invited_by)
        updated=replace(resource,owner=target,generation=resource.generation+1,
                        modified_at=ctx.now,modified_by=ctx.principal.actor)
        await tx.replace(updated,resource.generation)
        return output_for(updated,owner=target)

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
        from msg.storage.capacity import require_csr_capacity
        require_csr_capacity(tx)
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

    all_types=('topic','post','template','file','attachment','tool','user','organization','certificate','csr','delegation','repo','website','keystore','skill','legacy_directive','todo')
    types=[ResourceTypeSpec(name=name,version=1,container=name in {'topic','user','organization','repo','website'},
        content_schema=None,operations=frozenset(),relations=frozenset({'reply_to','thread_root','quote','repost','attachment','template'}),
        taggable=name in {'post','topic','repo'},purchasable=name=='website') for name in all_types]
    finish(types)
