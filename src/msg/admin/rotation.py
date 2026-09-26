"""Explicit local trust rotation with a PIN-encrypted, crash-recoverable journal.

The journal never contains a plaintext private key. Installing a new trust anchor
invalidates all old certificate chains for new requests. Historical public keys
and signed records remain available; they are not retroactively re-signed.
"""
from __future__ import annotations
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from msg.constants import ROOT_SUBJECT, ONLINE_CA, ADMINS_GROUP, CSR_SPACE
from msg.core.codec import canonical, decode, digest, wire, b64, unb64, loads
from msg.core.errors import require
from msg.core.models import Certificate, CertificateRequest, Credential, Signature, IssuancePolicy, AuditEvent, Event, ResourceRef
from msg.plugins.common import new_id
from msg.plugins.identity import certificate_resource
from msg.security.crypto import seal_private_key, open_private_key, Ed25519Signer
from msg.security.certificates import sign_certificate, csr_body
from msg.security.capabilities import grant_for
from msg.storage.git import durable_write
from msg.bootstrap import seed_resource


def journal_path(app):
    return app.settings.config_dir/'root'/'rotation.pending.json'


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
            max_child_ca_depth=8,max_delegation_depth=8),
        signature=Signature(key_id=new_signer.key_id, algorithm='ed25519', value=b''))
    new = sign_certificate(new, new_signer)
    statement = {'old_certificate':old.resource_id, 'old_fingerprint':digest(app.certificates.root_public_key),
        'new_certificate':new.resource_id, 'new_fingerprint':digest(new_signer.public_key), 'time':wire(now),
        'operator':operator, 'previous_key_proved':old_signer is not None}
    online = app.online_signer
    csr = CertificateRequest(resource_id=new_id('csr'), applicant=ONLINE_CA, subject_id=ONLINE_CA,
        requested_issuer=ROOT_SUBJECT, public_key=online.public_key, kind='ca',
        grants=(grant_for(app.registry.capability('cert.issue')),),
        issuance=IssuancePolicy(issue_grants=app.base_grants(),max_cert_ttl_seconds=app.settings.base_certificate_ttl,
            max_child_ca_depth=0,max_delegation_depth=8), requested_ttl_seconds=31536000,
        target_service=app.settings.service_url,delegation_depth=0,authority_sources=(),request_digest='',
        possession_proof=Signature(key_id=online.key_id,algorithm='ed25519',value=b''))
    csr = replace(csr, request_digest=digest(csr_body(csr)),
                  possession_proof=online.sign(canonical(csr_body(csr)),purpose='csr'))
    journal={'version':1,'old_certificate':wire(old),'new_trust':{'version':1,'public_key':b64(new_signer.public_key),
        'certificate':wire(new)},'new_envelope':seal_private_key(new_signer.private_bytes(),new_pin),
        'online_csr':wire(csr),'statement':statement,
        'new_signature':wire(new_signer.sign(canonical(statement),purpose='root-rotation')),
        'old_signature':wire(old_signer.sign(canonical(statement),purpose='root-rotation')) if old_signer else None}
    durable_write(journal_path(app),canonical(journal),mode=0o600)
    return journal


async def complete(app, journal, *, pin):
    from msg.security.crypto import verify
    private=open_private_key(journal['new_envelope'],pin)
    signer=Ed25519Signer.from_bytes(private)
    require(signer.public_key==unb64(journal['new_trust']['public_key']),'root_key_mismatch')
    require(journal['version']==1,'unknown_rotation_journal')
    verify(signer.public_key,canonical(journal['statement']),decode(Signature,journal['new_signature']),purpose='root-rotation')
    old=decode(Certificate,journal['old_certificate'])
    new=decode(Certificate,journal['new_trust']['certificate'])
    require(new.subject_id==ROOT_SUBJECT and new.target_service==app.settings.service_url,'rotation_service_mismatch')
    csr=decode(CertificateRequest,journal['online_csr'])
    # This method is called only after the local console has approved the exact
    # journal. It also handles the state where SQLite committed but files did not.
    await app.open_storage()
    async with app.metadata.transaction(write=True) as tx:
        current=tx.setting('active_root_certificate','cert_root')
        require(current in {old.resource_id,new.resource_id},'trust_anchor_mismatch')
        if current != new.resource_id:
            existing=await tx.certificate(old.resource_id)
            require(canonical(existing)==canonical(old),'trust_anchor_mismatch')
            old_key=await tx.credential(old.key_id)
            if journal['old_signature']:
                verify(old_key.verifier,canonical(journal['statement']),decode(Signature,journal['old_signature']),purpose='root-rotation')
            subject=await tx.subject(ROOT_SUBJECT)
            require(subject.local_only,'root_policy_corrupt')
            await tx.save_credential(Credential(id=signer.key_id,subject_id=ROOT_SUBJECT,kind='signing_key',
                verifier=signer.public_key,ceiling=(),not_before=new.not_before,expires_at=None,revoked_at=None),subject.auth_version)
            await tx.save_credential(replace(old_key,revoked_at=app.clock()),subject.auth_version)
            await certificate_resource(tx,new,app.clock())
            await tx.register_certificate(new,None,0)
            event=Event(id=new_id('audit'),type='root.rotate',time=app.clock(),request_id=new.resource_id,
                actor=ROOT_SUBJECT,subject=ROOT_SUBJECT,resources=(ResourceRef(id=new.resource_id),),
                data={'statement':journal['statement'],'new_signature':journal['new_signature'],
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
    protected=app.settings.config_dir/'root'
    history=protected/'history'/old.resource_id
    history.mkdir(parents=True,exist_ok=True,mode=0o700)
    key_file=protected/'key.json'
    if key_file.exists() and not (history/'key.json').exists():
        durable_write(history/'key.json',key_file.read_bytes(),mode=0o600)
    durable_write(key_file,canonical(journal['new_envelope']),mode=0o600)
    durable_write(app.settings.trust_file,canonical(journal['new_trust']),mode=0o444)
    # Journal survives every earlier failure and is removed only after both
    # metadata and protected files have durable copies.
    journal_path(app).unlink(missing_ok=True)
    return {'root_id':ROOT_SUBJECT,'certificate_id':new.resource_id,'fingerprint':digest(signer.public_key),
        'online_ca_request':csr.resource_id,'restart_required':True,'old_certificate_chains_valid':False}
