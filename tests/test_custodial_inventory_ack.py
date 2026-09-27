"""A custodial exit inventories old ciphertexts and binds client ACKs to mappings."""
import os
import shutil

import pytest
import httpx

from msg.client import ClientState,MsgClient
from msg.client_recovery import _age,ack_custodial_rewrap_entry
from msg.core.codec import b64,canonical,digest,loads,unb64,wire
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from test_custodial_rewrap import _trusted_age_entry
from test_custodial_upgrade import begin,finish
from test_service import NOW,call


@pytest.mark.asyncio
async def test_new_age_revision_after_start_blocks_finish_and_keeps_old_authority(installed):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app,_=installed
    recovery_secret=b64(os.urandom(32))
    created=await call(app,'identity.custodial_create',{
        'handle':'inventory-drift','nonce':b64(os.urandom(32)),
        'recovery_secret':recovery_secret},contract_version=2)
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    signer=Ed25519Signer.generate()
    new_identity,new_recipient=generate_age_key()
    challenge=await begin(app,subject,token,signer,new_recipient)
    ciphertext=_age('--encrypt','--recipient',created.data['encryption_recipient'],
                    input_data=b'late ciphertext')
    await _trusted_age_entry(app,subject,ciphertext,suffix='late')
    denied=await finish(app,subject,token,signer,new_identity,challenge.data)
    assert denied.status=='error' and denied.error.code=='custodial_inventory_changed'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind=='custodial'
        assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(subject,))[0]=='active'


@pytest.mark.asyncio
async def test_rewrap_mapping_requires_new_signer_ack_and_retains_historical_vault(installed):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app,_=installed
    recovery_secret=b64(os.urandom(32))
    created=await call(app,'identity.custodial_create',{
        'handle':'inventory-ack','nonce':b64(os.urandom(32)),
        'recovery_secret':recovery_secret},contract_version=2)
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    plaintext=b'private mapped bytes\n'
    ciphertext=_age('--encrypt','--recipient',created.data['encryption_recipient'],
                    input_data=plaintext)
    rid,old_revision=await _trusted_age_entry(app,subject,ciphertext)
    signer=Ed25519Signer.generate()
    new_identity,new_recipient=generate_age_key()
    challenge=await begin(app,subject,token,signer,new_recipient)
    challenge_id=challenge.data['challenge_id']
    pending=await finish(app,subject,token,signer,new_identity,challenge.data)
    assert wire(pending.data)['age_inventory']==[{'id':rid,'revision':old_revision,
                                                  'ciphertext_digest':digest(ciphertext)}]
    rewrapped=await call(app,'identity.custodial_rewrap_entry',{
        'challenge_id':challenge_id,'ciphertext_ref':{'id':rid,'revision':old_revision},
        'old_encryption_key_id':created.data['encryption_key_id'],
        'new_recipient':new_recipient},subject=subject,token=token,expected=((rid,1),))
    assert rewrapped.status=='ok',wire(rewrapped)
    mapping=rewrapped.data['mapping']
    inventory=await call(app,'identity.custodial_upgrade_inventory',
                           {'challenge_id':challenge_id},subject=subject,token=token)
    assert inventory.status=='ok' and not inventory.data['inventory_changed'],(
        inventory.error.code if inventory.error else wire(inventory))
    assert inventory.data['mappings'][old_revision]==mapping
    assert inventory.data['finalize_ready'] is False
    rid_ack='client-ack'
    signed={'subject_id':subject,'challenge_id':challenge_id,
            'old':mapping['old'],'new':mapping['new'],
            'plaintext_digest':digest(plaintext),'request_id':rid_ack}
    wrong=Ed25519Signer.generate().sign(canonical(signed),purpose='custodial-rewrap-ack')
    args={'challenge_id':challenge_id,'old_revision':old_revision,
          'new_revision':mapping['new']['revision'],
          'ciphertext_digest':mapping['new']['ciphertext_digest'],
          'plaintext_digest':digest(plaintext),'decryption_ack':wire(wrong)}
    denied=await call(app,'identity.custodial_rewrap_ack',args,
                      subject=subject,token=token,rid=rid_ack)
    assert denied.status=='error' and denied.error.code=='invalid_signature'
    args['decryption_ack']=wire(signer.sign(canonical(signed),purpose='custodial-rewrap-ack'))
    accepted=await call(app,'identity.custodial_rewrap_ack',args,
                        subject=subject,token=token,rid=rid_ack)
    assert accepted.status=='ok' and accepted.data['client_decryption_acked']
    inventory=await call(app,'identity.custodial_upgrade_inventory',
                           {'challenge_id':challenge_id},subject=subject,token=token)
    assert inventory.data['acks'][old_revision]['plaintext_digest']==digest(plaintext)
    assert inventory.data['finalize_ready'] is False
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind=='custodial'
        assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(subject,))[0]=='active'


@pytest.mark.asyncio
async def test_client_keeps_challenge_and_decrypts_locally_before_ack(installed,tmp_path):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app,_=installed
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                           base_url=app.settings.service_url)
    state=ClientState(tmp_path/'client',server=app.settings.service_url)
    client=MsgClient(state,HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
    try:
        created=await client.custodial('client-inventory')
        plaintext=b'client-owned ciphertext\n'
        ciphertext=_age('--encrypt','--recipient',
                        created.data['encryption_recipient'],input_data=plaintext)
        rid,old_revision=await _trusted_age_entry(app,state.subject,ciphertext)
        pending=await client.upgrade_custodial('client-inventory-self',
                                               external_ciphertexts_migrated=True)
        assert pending.status=='ok' and pending.data['status']=='pending_rewrap'
        journal=loads((state.directory/'custodial-upgrade.json').read_bytes())
        challenge_id=journal['challenge']['challenge_id']
        assert journal['status']=='pending_rewrap' and state.token is not None
        result=await client.call('identity.custodial_rewrap_entry',{
            'challenge_id':challenge_id,
            'ciphertext_ref':{'id':rid,'revision':old_revision},
            'old_encryption_key_id':created.data['encryption_key_id'],
            'new_recipient':state.encryption_recipient},expected=((rid,1),))
        assert result.status=='ok',wire(result)
        ack=await ack_custodial_rewrap_entry(client,challenge_id,old_revision,
                                            state.age_key_path)
        assert ack.status=='ok' and ack.data['client_decryption_acked']
        resumed=await client.upgrade_custodial('client-inventory-self',
                                               external_ciphertexts_migrated=True)
        assert resumed.status=='ok' and resumed.data['challenge_id']==challenge_id
        assert resumed.data['acks'][old_revision]['plaintext_digest']==digest(plaintext)
        assert resumed.data['finalize_ready'] is False
        assert loads((state.directory/'custodial-upgrade.json').read_bytes())['challenge'][
            'challenge_id']==challenge_id
        assert state.token is not None
    finally:
        await http.aclose()
