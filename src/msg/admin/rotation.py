"""Explicit local trust rotation with a PIN-encrypted, crash-recoverable journal.

The journal never contains a plaintext private key. Installing a new trust anchor
invalidates all old certificate chains for new requests. Historical public keys
and signed records remain available; they are not retroactively re-signed.
"""
from __future__ import annotations
from dataclasses import replace
from datetime import timedelta
import os

from msg.constants import ROOT_SUBJECT, ONLINE_CA, ADMINS_GROUP, CSR_SPACE
from msg.core.codec import canonical, decode, digest, wire, b64
from msg.core.errors import require
from msg.core.models import Certificate, CertificateRequest, Credential, Signature, IssuancePolicy, AuditEvent, Event, ResourceRef
from msg.plugins.common import new_id
from msg.plugins.identity import certificate_resource
from msg.security.crypto import seal_private_key, Ed25519Signer
from msg.security.certificates import sign_certificate, csr_body
from msg.security.rotation_journal import authorization, validate as validate_journal
from msg.security.capabilities import grant_for
from msg.storage.git import durable_write
from msg.bootstrap import seed_resource


def journal_path(app):
    from msg.admin.root import root_envelope
    return root_envelope(app.settings.config_dir).parent/'rotation.pending.json'


def prepare(app, new_signer, new_pin, *, old_signer, operator):
    require(not journal_path(app).exists(), 'root_rotation_pending')
    old = app.certificates.root_certificate
    if old_signer is not None:
        require(old_signer.key_id == old.key_id, 'root_key_mismatch')
    now = app.clock()
    grants = app.primary_ceiling()
    new = Certificate(resource_id=new_id('cert_root'), serial=new_id('serial'), subject_id=ROOT_SUBJECT,
        key_id=new_signer.key_id, issuer_id=ROOT_SUBJECT, parent_certificate_id=None, authority_sources=(), kind='ca',
        grants=grants, not_before=now, expires_at=now+timedelta(days=3650), target_service=app.settings.service_url,
        delegation_depth=8, issuance=IssuancePolicy(issue_grants=grants,max_cert_ttl_seconds=31536000,
            max_child_ca_depth=3,max_delegation_depth=8),
        signature=Signature(key_id=new_signer.key_id, algorithm='ed25519', value=b''))
    new = sign_certificate(new, new_signer)
    statement = {'old_certificate':old.resource_id, 'old_fingerprint':digest(app.certificates.root_public_key),
        'new_certificate':new.resource_id, 'new_fingerprint':digest(new_signer.public_key), 'time':wire(now),
        'operator':operator, 'previous_key_proved':old_signer is not None}
    online = app.online_signer
    csr = CertificateRequest(resource_id=new_id('csr'), applicant=ONLINE_CA, subject_id=ONLINE_CA,
        requested_issuer=ROOT_SUBJECT, public_key=online.public_key, kind='ca',
        grants=(grant_for(app.registry.capability('cert.issue'),scope=app.default_scope()),),
        issuance=IssuancePolicy(issue_grants=app.base_grants(),max_cert_ttl_seconds=app.settings.base_certificate_ttl,
            max_child_ca_depth=0,max_delegation_depth=8), requested_ttl_seconds=31536000,
        target_service=app.settings.service_url,delegation_depth=0,authority_sources=(),request_digest='',
        possession_proof=Signature(key_id=online.key_id,algorithm='ed25519',value=b''))
    csr = replace(csr, request_digest=digest(csr_body(csr)),
                  possession_proof=online.sign(canonical(csr_body(csr)),purpose='csr'))
    from msg.admin.root import root_envelope
    key_file=root_envelope(app.settings.config_dir)
    require(not key_file.is_symlink(),'unsafe_root_private_path')
    journal={'version':2,'old_certificate':wire(old),'new_trust':{'version':1,
        'public_key':b64(new_signer.public_key),'certificate':wire(new)},
        'new_envelope':seal_private_key(new_signer.private_bytes(),new_pin),
        'previous_envelope_digest':digest(key_file.read_bytes()) if key_file.exists() else None,
        'online_csr':wire(csr),'statement':statement}
    proof=canonical(authorization(journal))
    journal['new_signature']=wire(new_signer.sign(proof,purpose='root-rotation'))
    journal['old_signature']=wire(old_signer.sign(proof,purpose='root-rotation')) if old_signer else None
    protected=journal_path(app).parent
    require(not protected.is_symlink(),'unsafe_root_private_path')
    protected.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(protected,0o700)
    durable_write(journal_path(app),canonical(journal),mode=0o600)
    return journal


async def complete(app, journal, *, pin):
    from msg.security.crypto import verify
    pending=journal_path(app)
    protected=pending.parent
    require(not protected.is_symlink() and not pending.is_symlink(),
            'unsafe_root_private_path')
    online=Ed25519Signer.from_bytes((app.settings.service_keys/'online.key').read_bytes())
    signer,old,new,csr,proof=validate_journal(journal,pin=pin,
        service=app.settings.service_url,online_public=online.public_key)
    key_file=protected/'key.json'
    require(not key_file.is_symlink(),'unsafe_root_private_path')
    if key_file.exists():
        require(digest(key_file.read_bytes()) in {
            journal['previous_envelope_digest'],digest(canonical(journal['new_envelope']))},
            'root_envelope_changed')
    else:
        require(journal['previous_envelope_digest'] is None,'root_envelope_missing')
    # This method is called only after the local console has approved the exact
    # journal. It also handles the state where PostgreSQL committed but protected files did not.
    await app.open_storage()
    async with app.metadata.transaction(write=True) as tx:
        current=tx.setting('active_root_certificate','cert_root')
        require(current in {old.resource_id,new.resource_id},'trust_anchor_mismatch')
        existing=await tx.certificate(old.resource_id)
        require(canonical(existing)==canonical(old),'trust_anchor_mismatch')
        old_key=await tx.credential(old.key_id)
        require(old_key.subject_id==ROOT_SUBJECT and
                digest(old_key.verifier)==journal['statement']['old_fingerprint'],
                'rotation_statement_mismatch')
        if journal['old_signature'] is not None:
            verify(old_key.verifier,canonical(proof),decode(Signature,journal['old_signature']),
                   purpose='root-rotation')
        if current == new.resource_id:
            require(canonical(await tx.certificate(new.resource_id))==canonical(new) and
                    canonical(await tx.csr(csr.resource_id))==canonical(csr) and
                    old_key.revoked_at is not None and await tx.certificate_revoked(old.resource_id),
                    'rotation_resume_mismatch')
            installed=await tx.credential(new.key_id)
            require(installed.verifier==signer.public_key and installed.revoked_at is None,
                    'rotation_resume_mismatch')
        else:
            subject=await tx.subject(ROOT_SUBJECT)
            require(subject.local_only,'root_policy_corrupt')
            await tx.save_credential(Credential(id=signer.key_id,subject_id=ROOT_SUBJECT,kind='signing_key',
                verifier=signer.public_key,ceiling=(),not_before=new.not_before,expires_at=None,revoked_at=None),subject.auth_version)
            await tx.save_credential(replace(old_key,revoked_at=app.clock()),subject.auth_version)
            await certificate_resource(tx,new,app.clock())
            await tx.register_certificate(new,None,0)
            event=Event(id=new_id('audit'),type='root.rotate',time=app.clock(),request_id=new.resource_id,
                actor=ROOT_SUBJECT,subject=ROOT_SUBJECT,resources=(ResourceRef(id=new.resource_id),),
                data={'statement':journal['statement'],'authorization':proof,'new_signature':journal['new_signature'],
                      'old_signature':journal['old_signature']})
            await tx.revoke_certificate(old.resource_id,AuditEvent(event=event,authority=(),
                before_digest=digest(old),after_digest=digest(new),previous_digest=None,entry_digest='',result='root_rotated'))
            await seed_resource(tx,app.contents,dict(id=csr.resource_id,type='csr',name=csr.resource_id,parent=CSR_SPACE,
                owner=ONLINE_CA,group=ADMINS_GROUP,mode='0600'),app.clock())
            await tx.save_csr(csr)
            tx.set_setting('online_ca_request',csr.resource_id)
            tx.set_setting('online_ca_certificate',None)
            tx.set_setting('active_root_certificate',new.resource_id)
            tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)+1)
            tx.set_setting('root_rotation:'+new.resource_id,journal['statement'])
    # The journal and envelope must use the same independently protected root
    # directory, including when resuming after the metadata commit.
    for directory in (protected,protected/'history',protected/'history'/old.resource_id):
        require(not directory.is_symlink(),'unsafe_root_private_path')
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(directory,0o700)
    history=protected/'history'/old.resource_id/'key.json'
    require(not history.is_symlink(),'unsafe_root_private_path')
    if history.exists():
        require(digest(history.read_bytes())==journal['previous_envelope_digest'],
                'rotation_history_mismatch')
    elif journal['previous_envelope_digest'] is not None:
        # Never archive an already installed new envelope as the old key.
        require(key_file.exists() and
                digest(key_file.read_bytes())==journal['previous_envelope_digest'],
                'rotation_history_missing')
        durable_write(history,key_file.read_bytes(),mode=0o600)
    durable_write(key_file,canonical(journal['new_envelope']),mode=0o600)
    durable_write(app.settings.trust_file,canonical(journal['new_trust']),mode=0o444)
    # Journal survives every earlier failure and is removed only after both
    # metadata and protected files have durable copies.
    pending.unlink(missing_ok=True)
    directory=os.open(protected,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    return {'root_id':ROOT_SUBJECT,'certificate_id':new.resource_id,'fingerprint':digest(signer.public_key),
        'online_ca_request':csr.resource_id,'restart_required':True,'old_certificate_chains_valid':False}
