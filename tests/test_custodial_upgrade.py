"""Custodial exit requires both new private keys and preserves recovery on failure."""
import os

import pytest

from msg.core.codec import b64,canonical,digest,unb64,wire
from msg.core.models import ResourceRef
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer,verify
from msg.security.vault import client_upgrade_proof
from test_service import call


async def begin(app,subject,token,signer,recipient, *, handle='new-self',rid='upgrade-start'):
    public=b64(signer.public_key)
    signed={'subject_id':subject,'handle':handle,'public_key':public,
            'encryption_recipient':recipient,'request_id':rid}
    proof=signer.sign(canonical(signed),purpose='custodial-upgrade-start')
    return await call(app,'identity.custodial_upgrade_start',{'handle':handle,
        'public_key':public,'encryption_recipient':recipient,
        'possession_proof':wire(proof)},subject=subject,token=token,rid=rid)


async def finish(app,subject,token,signer,age_identity,challenge, *, rid='upgrade-finish',
                 external_migrated=True,age_proof=None):
    age_proof=age_proof or client_upgrade_proof(age_identity,challenge)
    signed={'subject_id':subject,'challenge_id':challenge['challenge_id'],
            'age_proof':age_proof,'external_ciphertexts_migrated':external_migrated,
            'request_id':rid}
    acknowledgement=signer.sign(canonical(signed),purpose='custodial-upgrade-finish')
    return await call(app,'identity.custodial_upgrade_finish',{
        'challenge_id':challenge['challenge_id'],'age_proof':age_proof,
        'external_ciphertexts_migrated':external_migrated,
        'migration_ack':wire(acknowledgement)},subject=subject,token=token,rid=rid)


@pytest.mark.asyncio
async def test_wrong_age_proof_fails_without_losing_old_token_or_vault(installed):
    app,_=installed
    created=await call(app,'identity.custodial_create',{'handle':'cust-proof',
        'nonce':b64(os.urandom(32))})
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    signer=Ed25519Signer.generate()
    age_identity,recipient=generate_age_key()
    challenge=await begin(app,subject,token,signer,recipient)
    assert challenge.status=='ok' and challenge.data['status']=='pending',wire(challenge)
    wrong=await finish(app,subject,token,signer,age_identity,challenge.data,
                       age_proof=b64(os.urandom(32)))
    assert wrong.status=='error' or wrong.data.get('status')=='failed'
    still=await call(app,'identity.custodial_status',{},subject=subject,token=token)
    assert still.status=='ok' and still.data['server_signable']
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind=='custodial'
        assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(subject,))[0]=='active'


@pytest.mark.asyncio
async def test_wrong_new_signer_ack_consumes_challenge_without_revoking_token(installed):
    app,_=installed
    created=await call(app,'identity.custodial_create',{'handle':'cust-ack',
        'nonce':b64(os.urandom(32))})
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    signer=Ed25519Signer.generate()
    age_identity,recipient=generate_age_key()
    challenge=await begin(app,subject,token,signer,recipient,rid='ack-start')
    wrong=await finish(app,subject,token,Ed25519Signer.generate(),age_identity,
                       challenge.data,rid='wrong-ack')
    assert wrong.status=='ok' and wrong.data['status']=='failed'
    replay=await finish(app,subject,token,signer,age_identity,challenge.data,rid='correct-after-fail')
    assert replay.status=='error' and replay.error.code=='custodial_upgrade_not_pending'
    assert (await call(app,'identity.custodial_status',{},subject=subject,token=token)).status=='ok'


@pytest.mark.asyncio
async def test_server_tracked_age_ciphertext_keeps_upgrade_pending(installed):
    app,_=installed
    created=await call(app,'identity.custodial_create',{'handle':'cust-pending',
        'nonce':b64(os.urandom(32))})
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    # This test inserts a keystore age revision through the trusted store to
    # exercise the migration gate; token keystore.put is intentionally disabled.
    async with app.metadata.transaction(write=True) as tx:
        from msg.core.models import Resource,Revision
        from dataclasses import replace
        from msg.core.codec import digest
        parent=tx.one("SELECT id FROM resources WHERE parent=? AND name='keystore'",(subject,))[0]
        rid='ks_pending_'+subject[-12:]
        resource=Resource(id=rid,type='keystore',type_version=1,name='old.age',parent=parent,
            owner=subject,group='g_public',mode=0o600,generation=0,revision=None,state='active',
            created_at=app.clock(),created_by=subject,modified_at=app.clock(),modified_by=subject)
        await tx.insert(resource)
        blob=await app.contents.put_bytes(b'age-encryption.org/v1\nold ciphertext','application/octet-stream')
        revision=Revision(format_version=1,id='v_'+subject[-24:],resource_id=rid,parents=(),
            content=blob,relations=(),actor=subject,subject=subject,author=subject,
            created_at=app.clock(),manifest_digest='')
        revision=replace(revision,manifest_digest=digest({k:v for k,v in wire(revision).items()
            if k not in {'manifest_digest','signature'}}))
        await app.contents.pin(blob,revision.id)
        await app.contents.commit_revision(parent,revision)
        await tx.append_revision(revision)
        await tx.replace(replace(resource,generation=1,revision=revision.id),0)
        tx.set_setting('keystore_format:'+revision.id,'age')
    signer=Ed25519Signer.generate()
    age_identity,recipient=generate_age_key()
    challenge=await begin(app,subject,token,signer,recipient,rid='pending-start')
    pending=await finish(app,subject,token,signer,age_identity,challenge.data,rid='pending-finish')
    assert pending.status=='ok' and pending.data['status']=='pending_rewrap'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind=='custodial'
        assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(subject,))[0]=='active'
    assert (await call(app,'identity.custodial_status',{},subject=subject,token=token)).status=='ok'


@pytest.mark.asyncio
async def test_empty_custodial_upgrade_switches_atomically_and_replays_result(installed):
    app,_=installed
    created=await call(app,'identity.custodial_create',{'handle':'cust-empty',
        'nonce':b64(os.urandom(32))})
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    old_post=await call(app,'content.post_create',{'parent':'/main','body':'old signature'},
                        subject=subject,token=token)
    assert old_post.status=='ok'
    signer=Ed25519Signer.generate()
    age_identity,recipient=generate_age_key()
    challenge=await begin(app,subject,token,signer,recipient,rid='empty-start')
    completed=await finish(app,subject,token,signer,age_identity,challenge.data,rid='empty-finish')
    assert completed.status=='ok' and completed.data['status']=='completed',wire(completed)
    assert completed.data['subject_id']==subject
    replay=await finish(app,subject,token,signer,age_identity,challenge.data,rid='empty-finish')
    assert replay.status=='ok' and replay.replayed
    recovered=await call(app,'identity.custodial_upgrade_result',
                         {'challenge_id':challenge.data['challenge_id']},
                         key=signer,subject=subject)
    assert recovered.status=='ok' and recovered.data['status']=='completed'
    assert recovered.data['certificate_id']==completed.data['certificate_id']
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind=='registered'
        old_token=await tx.credential(token[0])
        assert old_token.revoked_at is not None
        row=tx.one('SELECT status,signing_ciphertext,age_ciphertext FROM custodial_vault WHERE subject=?',
                   (subject,))
        assert row==('destroyed',None,None)
        original=await tx.revision(ResourceRef(id=old_post.resources[0].id))
        public=await tx.credential(original.signature.key_id)
        body={k:v for k,v in wire(original).items() if k not in {'manifest_digest','signature'}}
        verify(public.verifier,canonical(body),original.signature,purpose='revision')
        assert tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=? AND is_primary=1',(subject,))[0]==1
        assert tx.one('SELECT COUNT(*) FROM encryption_subkeys WHERE subject=? AND is_primary=1',(subject,))[0]==1
    rejected=await call(app,'content.post_create',{'parent':'/main','body':'old token denied'},
                        subject=subject,token=token)
    assert rejected.status=='error'
    new_post=await call(app,'content.post_create',{'parent':'/main','body':'new self custody'},
                        subject=subject,key=signer)
    assert new_post.status=='ok'
