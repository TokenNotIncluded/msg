"""Custodial subjects have separate encrypted keys and bounded token authority."""
import os
import shutil
import subprocess

import pytest

from msg.core.codec import b64,canonical,unb64,wire
from msg.core.models import ResourceRef
from msg.security.crypto import verify
from msg.core.requests import receipt_bytes
from test_service import call,register


@pytest.mark.asyncio
async def test_custodial_bootstrap_encrypts_two_keys_and_signs_revision(installed):
    app, _ = installed
    args={'handle':'vault-agent','nonce':b64(os.urandom(32))}
    created=await call(app,'identity.custodial_create',args,rid='vault-bootstrap')
    assert created.status=='ok',wire(created)
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    assert created.data['signature_source']=='custodial'
    assert created.data['server_signable'] and created.data['server_decryptable']
    verify(app.receipt_signer.public_key,receipt_bytes(created),created.receipt,purpose='receipt')
    replay=await call(app,'identity.custodial_create',args,rid='vault-bootstrap')
    assert replay.status=='ok' and replay.replayed and replay.data['token']==created.data['token']
    status=await call(app,'identity.custodial_status',{},subject=subject,token=token)
    assert status.status=='ok' and status.data['server_signable'] and status.data['server_decryptable']
    posted=await call(app,'content.post_create',{'parent':'/main','body':'server held signing key'},
                      subject=subject,token=token)
    assert posted.status=='ok',wire(posted)
    edited=await call(app,'content.post_edit',{'id':posted.resources[0].id,
        'expected_revision':posted.resources[0].revision,'body':'server held edit'},
        subject=subject,token=token,
        expected=((posted.resources[0].id,posted.data['generation']),))
    assert edited.status=='ok',wire(edited)
    async with app.metadata.transaction(write=False) as tx:
        identity=tx.one('SELECT key_id,public_key FROM identity_keys WHERE subject=?',(subject,))
        age=tx.one('SELECT key_id,recipient FROM encryption_subkeys WHERE subject=?',(subject,))
        vault=tx.one('SELECT signing_ciphertext,age_ciphertext FROM custodial_vault WHERE subject=?',(subject,))
        assert identity and age and vault and identity[0]!=age[0]
        assert (await tx.subject(subject)).kind=='custodial'
        assert token[1] not in str(vault).encode()
        from msg.security.vault import open_signer,open_age_identity
        signer=open_signer(app,tx,subject)
        secret=open_age_identity(app,tx,subject)
        assert b64(signer.private_bytes()) not in str(vault)
        assert secret not in str(vault)
        if shutil.which('age'):
            identity_file=__import__('pathlib',fromlist=['Path']).Path(app.settings.config_dir)/'custodial-test.agekey'
            identity_file.write_text(secret+'\n')
            identity_file.chmod(0o600)
            try:
                encrypted=subprocess.check_output(['age','-e','-r',age[1]],input=b'vault decrypt test')
                recovered=subprocess.check_output(['age','-d','-i',str(identity_file)],input=encrypted)
                assert recovered==b'vault decrypt test'
            finally:
                identity_file.unlink()
        revision=await tx.revision(ResourceRef(id=posted.resources[0].id))
        assert revision.signature_source=='custodial' and revision.signature is not None
        assert revision.parents==(posted.resources[0].revision,)
        body={k:v for k,v in wire(revision).items() if k not in {'manifest_digest','signature'}}
        verify(signer.public_key,canonical(body),revision.signature,purpose='revision')
        credential=await tx.credential(token[0])
        assert credential.verifier!=token[1] and credential.subject_id==subject
        assert credential.expires_at is not None and credential.ceiling
        assert all(grant.scope.resource_id==app.namespace_root and not grant.constraints
                   for grant in credential.ceiling)
    direct=await call(app,'content.post_create',{'parent':'/main','body':'signer alone cannot authorize'},
                      key=signer,subject=subject)
    assert direct.status=='error' and direct.error.code=='credential_ceiling'


@pytest.mark.asyncio
async def test_custodial_token_scope_and_upgrade_fail_closed(installed):
    app, _ = installed
    created=await call(app,'identity.custodial_create',{'handle':'vault-limits',
        'nonce':b64(os.urandom(32))})
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    wrong=await call(app,'content.post_create',{'parent':'/main','body':'wrong token'},
                     subject=subject,token=(token[0],os.urandom(32)))
    assert wrong.status=='error'
    forbidden=await call(app,'content.chmod',{'id':'/private','mode':'0777'},
                         subject=subject,token=token)
    assert forbidden.status=='error'
    signing_key=__import__('msg.security.crypto',fromlist=['Ed25519Signer']).Ed25519Signer.generate()
    age_recipient=__import__('msg.security.age_keys',fromlist=['generate_age_key']).generate_age_key()[1]
    args={'subject_id':subject,'handle':'vault-upgraded','public_key':b64(signing_key.public_key),
          'encryption_recipient':age_recipient}
    proof=signing_key.sign(canonical(args),purpose='upgrade')
    upgrade=await call(app,'identity.upgrade',{'handle':'vault-upgraded',
        'public_key':args['public_key'],'encryption_recipient':age_recipient,
        'possession_proof':wire(proof)},subject=subject,token=token,contract_version=2)
    assert upgrade.status=='error' and upgrade.error.code=='custodial_operation_not_supported'
    unsupported=await call(app,'communication.send',{'recipient':subject,
        'resource':{'id':'/main'}},subject=subject,token=token)
    assert unsupported.status=='error' and unsupported.error.code=='custodial_operation_not_supported'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind=='custodial'
        assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(subject,))[0]=='active'


@pytest.mark.asyncio
async def test_custodial_rotation_and_vault_tamper_fail_closed(installed):
    app, _ = installed
    created=await call(app,'identity.custodial_create',{'handle':'vault-rotate',
        'nonce':b64(os.urandom(32))})
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    rotated=await call(app,'identity.token_rotate',{'nonce':b64(os.urandom(32))},
                       subject=subject,token=token,rid='vault-rotate-once')
    assert rotated.status=='ok' and rotated.data['token']
    new_token=(rotated.data['credential_id'],unb64(rotated.data['token']))
    old=await call(app,'content.post_create',{'parent':'/main','body':'old token'},
                   subject=subject,token=token)
    assert old.status=='error'
    signed=await call(app,'content.post_create',{'parent':'/main','body':'new token'},
                      subject=subject,token=new_token)
    assert signed.status=='ok'
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("UPDATE custodial_vault SET signing_ciphertext=? WHERE subject=?",
                   ('AAAA',subject),write=True)
        before=tx.one("SELECT COUNT(*) FROM resources WHERE type='post' AND owner=?",(subject,))[0]
    failed=await call(app,'content.post_create',{'parent':'/main','body':'must roll back'},
                      subject=subject,token=new_token)
    assert failed.status=='error' and failed.error.code=='custodial_vault_corrupt'
    status=await call(app,'identity.custodial_status',{},subject=subject,token=new_token)
    assert status.status=='error' and status.error.code=='custodial_vault_corrupt'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post' AND owner=?",(subject,))[0]==before


@pytest.mark.asyncio
async def test_lost_bootstrap_response_can_replay_same_request_without_new_token(installed):
    app, _ = installed
    args={'handle':'vault-lost-response','nonce':b64(os.urandom(32))}
    await call(app,'identity.custodial_create',args,rid='lost-response')  # response lost in transit
    recovered=await call(app,'identity.custodial_create',args,rid='lost-response')
    assert recovered.status=='ok' and recovered.replayed and recovered.data['token']
    token=(recovered.data['credential_id'],unb64(recovered.data['token']))
    work=await call(app,'content.post_create',{'parent':'/main','body':'continued after retry'},
                    subject=recovered.data['subject_id'],token=token)
    assert work.status=='ok'
