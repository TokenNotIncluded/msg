"""A new temporary subject binds independent client-held keys at creation."""
import hashlib
import os

import pytest

from msg.core.codec import b64, canonical, unb64, wire
from msg.security.age_keys import generate_age_key, public_from_recipient
from msg.security.crypto import Ed25519Signer
from test_service import call


def temporary_arguments(signer, recipient, *, nonce, request_id):
    subject='u_tmp_'+hashlib.sha256(unb64(nonce)).hexdigest()[:32]
    proof=signer.sign(canonical({'subject_id':subject,'nonce':nonce,
        'public_key':b64(signer.public_key),
        'encryption_recipient':recipient,'request_id':request_id}),
        purpose='temporary-key-possession-v1')
    return {'nonce':nonce,'recovery_secret':b64(os.urandom(32)),
            'public_key':b64(signer.public_key),
            'encryption_recipient':recipient,'possession_proof':wire(proof)}


@pytest.mark.asyncio
async def test_temporary_v3_creates_two_client_keys_and_upgrade_reuses_them(installed):
    app,_=installed
    signer=Ed25519Signer.generate()
    _,recipient=generate_age_key()
    assert signer.public_key!=public_from_recipient(recipient)
    request_id='temporary-dual-key'
    args=temporary_arguments(signer,recipient,nonce=b64(os.urandom(32)),
                             request_id=request_id)
    created=await call(app,'identity.temporary',args,rid=request_id,
                       contract_version=3)
    assert created.status=='ok',wire(created)
    subject=created.data['subject_id']
    assert created.data['encryption_recipient']==recipient
    assert created.data['server_signable'] is False
    assert created.data['server_decryptable'] is False
    async with app.metadata.transaction(write=False) as tx:
        signing=tx.one('SELECT key_id,public_key FROM identity_keys WHERE subject=?',
                       (subject,))
        encryption=tx.one('SELECT key_id,recipient,public_key FROM encryption_subkeys WHERE subject=?',
                          (subject,))
        assert signing[0]==created.data['identity_key_id']
        assert signing[1]==b64(signer.public_key)
        assert encryption[0]==created.data['encryption_key_id']
        assert encryption[1]==recipient
        assert encryption[2]==b64(public_from_recipient(recipient))
        assert tx.one('SELECT COUNT(*) FROM custodial_vault WHERE subject=?',(subject,))[0]==0
    replay=await call(app,'identity.temporary',args,rid=request_id,contract_version=3)
    assert replay.error.code=='token_delivery_unavailable'
    signed={'subject_id':subject,'handle':'temporary-promoted',
            'public_key':args['public_key'],'encryption_recipient':recipient}
    upgraded=await call(app,'identity.upgrade',{
        'handle':'temporary-promoted','public_key':args['public_key'],
        'encryption_recipient':recipient,
        'possession_proof':wire(signer.sign(canonical(signed),purpose='upgrade'))},
        subject=subject,
        token=(created.data['credential_id'],unb64(created.data['token'])),
        contract_version=2)
    assert upgraded.status=='ok',wire(upgraded)
    assert upgraded.data['subject_id']==subject
    assert upgraded.data['key_id']==created.data['identity_key_id']
    assert upgraded.data['encryption_key_id']==created.data['encryption_key_id']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=?',(subject,))[0]==1
        assert tx.one('SELECT COUNT(*) FROM encryption_subkeys WHERE subject=?',(subject,))[0]==1


@pytest.mark.asyncio
async def test_legacy_temporary_versions_and_false_key_proof_create_no_subject(installed):
    app,_=installed
    async with app.metadata.transaction(write=False) as tx:
        before=tx.one("SELECT COUNT(*) FROM identities WHERE kind='temporary'")[0]
    nonce=b64(os.urandom(32))
    for version,args in ((1,{'nonce':nonce}),
                         (2,{'nonce':nonce,'recovery_secret':b64(os.urandom(32))})):
        denied=await call(app,'identity.temporary',args,contract_version=version)
        assert denied.error.code=='temporary_dual_keys_required'
    signer=Ed25519Signer.generate()
    _,recipient=generate_age_key()
    args=temporary_arguments(signer,recipient,nonce=b64(os.urandom(32)),
                             request_id='bad-temporary-proof')
    args['public_key']=b64(Ed25519Signer.generate().public_key)
    denied=await call(app,'identity.temporary',args,rid='bad-temporary-proof',
                      contract_version=3)
    assert denied.status=='error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM identities WHERE kind='temporary'")[0]==before
