"""A pending custodial exit can convert one exact owned age revision."""
from dataclasses import replace
import os
import shutil

import pytest

from msg.client_recovery import _age
from msg.core.codec import b64,digest,wire,unb64
from msg.core.models import Resource,ResourceRef,Revision
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer
from msg.security.vault import open_age_identity
from test_custodial_upgrade import begin,finish
from test_service import call


async def _trusted_age_entry(app,subject,ciphertext, *, suffix='selected'):
    async with app.metadata.transaction(write=True) as tx:
        folder=tx.one("SELECT id FROM resources WHERE parent=? AND name='keystore'",
                      (subject,))[0]
        rid='ks_rewrap_'+suffix+'_'+subject[-12:]
        resource=Resource(id=rid,type='keystore',type_version=1,name=suffix+'.age',
            parent=folder,owner=subject,group='g_public',mode=0o600,generation=0,
            revision=None,state='active',created_at=app.clock(),created_by=subject,
            modified_at=app.clock(),modified_by=subject)
        await tx.insert(resource)
        blob=await app.contents.put_bytes(ciphertext,'application/octet-stream')
        revision=Revision(format_version=1,id='v_'+suffix+'_'+subject[-24:],resource_id=rid,
            parents=(),content=blob,relations=(),actor=subject,subject=subject,
            author=subject,created_at=app.clock(),manifest_digest='')
        revision=replace(revision,manifest_digest=digest({k:v for k,v in wire(revision).items()
            if k not in {'manifest_digest','signature'}}))
        await app.contents.pin(blob,revision.id)
        await app.contents.commit_revision(folder,revision)
        await tx.append_revision(revision)
        await tx.replace(replace(resource,generation=1,revision=revision.id),0)
        tx.set_setting('keystore_format:'+revision.id,'age')
    return rid,revision.id


@pytest.mark.asyncio
async def test_pending_rewrap_converts_only_selected_revision_and_keeps_vault(installed,tmp_path):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app,_=installed
    created=await call(app,'identity.custodial_create',{
        'handle':'cust-rewrap','nonce':b64(os.urandom(32))})
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    old_key_id=created.data['encryption_key_id']
    plaintext=b'one selected custodial ciphertext\n'
    ciphertext=_age('--encrypt','--recipient',created.data['encryption_recipient'],
                    input_data=plaintext)
    rid,revision=await _trusted_age_entry(app,subject,ciphertext)
    unrelated_identity,unrelated_recipient=generate_age_key()
    unrelated=_age('--encrypt','--recipient',unrelated_recipient,input_data=b'external')
    other_id,other_revision=await _trusted_age_entry(app,subject,unrelated,suffix='unrelated')
    signer=Ed25519Signer.generate()
    new_identity,new_recipient=generate_age_key()
    challenge=await begin(app,subject,token,signer,new_recipient)
    assert challenge.status=='ok',wire(challenge)
    pending=await finish(app,subject,token,signer,new_identity,challenge.data)
    assert pending.status=='ok' and pending.data['status']=='pending_rewrap',wire(pending)
    args={'challenge_id':challenge.data['challenge_id'],
          'ciphertext_ref':{'id':rid,'revision':revision},
          'old_encryption_key_id':old_key_id,'new_recipient':new_recipient}
    wrong=dict(args,old_encryption_key_id='ek_unknown')
    denied=await call(app,'identity.custodial_rewrap_entry',wrong,subject=subject,token=token,
                      expected=((rid,1),))
    assert denied.status=='error' and denied.error.code=='custodial_rewrap_key_mismatch'
    wrong_recipient=dict(args,new_recipient=created.data['encryption_recipient'])
    denied=await call(app,'identity.custodial_rewrap_entry',wrong_recipient,
                      subject=subject,token=token,expected=((rid,1),))
    assert denied.status=='error' and denied.error.code=='custodial_rewrap_key_mismatch'
    outsider=await call(app,'identity.custodial_create',{
        'handle':'cust-outsider','nonce':b64(os.urandom(32))})
    other_token=(outsider.data['credential_id'],unb64(outsider.data['token']))
    denied=await call(app,'identity.custodial_rewrap_entry',args,
                      subject=outsider.data['subject_id'],token=other_token,expected=((rid,1),))
    assert denied.status=='error' and denied.error.code in {
        'permission_denied','custodial_upgrade_not_pending_rewrap'}
    arbitrary=dict(args,ciphertext_ref={'id':other_id,'revision':other_revision})
    denied=await call(app,'identity.custodial_rewrap_entry',arbitrary,
                      subject=subject,token=token,expected=((other_id,1),))
    assert denied.status=='error' and denied.error.code=='custodial_rewrap_decrypt_failed'
    assert unrelated_identity
    done=await call(app,'identity.custodial_rewrap_entry',args,subject=subject,token=token,
                    expected=((rid,1),))
    assert done.status=='ok',wire(done)
    assert done.data['client_decryption_verified'] is False
    assert 'one selected' not in str(wire(done))
    new_revision=done.resources[0].revision
    assert new_revision!=revision
    old=await call(app,'keystore.get',{'id':rid,'revision':revision},subject=subject,token=token)
    new=await call(app,'keystore.get',{'id':rid,'revision':new_revision},subject=subject,token=token)
    assert old.status==new.status=='ok'
    async with app.metadata.transaction(write=False) as tx:
        old_blob=await app.contents.read_bytes((await tx.revision(ResourceRef(id=rid,revision=revision))).content)
        new_blob=await app.contents.read_bytes((await tx.revision(ResourceRef(id=rid,revision=new_revision))).content)
        old_identity=open_age_identity(app,tx,subject)
        assert (await tx.subject(subject)).kind=='custodial'
        assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(subject,))[0]=='active'
        assert tx.one("SELECT COUNT(*) FROM audit WHERE body LIKE '%custodial_rewrap_entry%'")[0]==1
    old_path=tmp_path/'old.agekey'
    new_path=tmp_path/'new.agekey'
    for path,identity in ((old_path,old_identity),(new_path,new_identity)):
        path.write_text(identity+'\n')
        path.chmod(0o600)
    assert _age('--decrypt','--identity',str(old_path),input_data=old_blob)==plaintext
    assert _age('--decrypt','--identity',str(new_path),input_data=new_blob)==plaintext
    stale=await call(app,'identity.custodial_rewrap_entry',args,subject=subject,token=token,
                     expected=((rid,1),))
    assert stale.status=='error' and stale.error.code in {'generation_conflict','revision_conflict'}
