"""Historical ciphertext stays immutable while a new-key copy is addressable."""
from dataclasses import replace
import os
import shutil

import pytest

from msg.client_recovery import _age
from msg.core.codec import b64,canonical,digest,unb64,wire
from msg.core.models import ResourceRef,Revision
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer
from test_custodial_rewrap import _trusted_age_entry
from test_custodial_upgrade import begin,finish
from test_service import call


@pytest.mark.asyncio
async def test_historical_revision_gets_independent_mapped_copy(installed,tmp_path):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app,_=installed
    created=await call(app,'identity.custodial_create',{
        'handle':'history-copy','nonce':b64(os.urandom(32))})
    subject=created.data['subject_id']
    token=(created.data['credential_id'],unb64(created.data['token']))
    old_plain=b'first historical secret\n'
    current_plain=b'second current secret\n'
    recipient=created.data['encryption_recipient']
    old_cipher=_age('--encrypt','--recipient',recipient,input_data=old_plain)
    rid,old_revision=await _trusted_age_entry(app,subject,old_cipher)
    current_cipher=_age('--encrypt','--recipient',recipient,input_data=current_plain)
    async with app.metadata.transaction(write=True) as tx:
        resource=await tx.resource(rid)
        blob=await app.contents.put_bytes(current_cipher,'application/octet-stream')
        current_revision='v_current_'+subject[-24:]
        revision=Revision(format_version=1,id=current_revision,resource_id=rid,
            parents=(old_revision,),content=blob,relations=(),actor=subject,subject=subject,
            author=subject,created_at=app.clock(),manifest_digest='')
        revision=replace(revision,manifest_digest=digest({k:v for k,v in wire(revision).items()
            if k not in {'manifest_digest','signature'}}))
        await app.contents.pin(blob,revision.id)
        await app.contents.commit_revision(resource.parent,revision)
        await tx.append_revision(revision)
        await tx.replace(replace(resource,generation=2,revision=revision.id),1)
        tx.set_setting('keystore_format:'+revision.id,'age')
    signer=Ed25519Signer.generate()
    new_identity,new_recipient=generate_age_key()
    challenge=await begin(app,subject,token,signer,new_recipient)
    pending=await finish(app,subject,token,signer,new_identity,challenge.data)
    assert pending.status=='ok' and pending.data['server_tracked_age_revisions']==2
    cid=challenge.data['challenge_id']
    migrated=await call(app,'identity.custodial_rewrap_revision',{
        'challenge_id':cid,'ciphertext_ref':{'id':rid,'revision':old_revision},
        'old_encryption_key_id':created.data['encryption_key_id'],
        'new_recipient':new_recipient},subject=subject,token=token)
    assert migrated.status=='ok',wire(migrated)
    mapping=migrated.data['mapping']
    assert mapping['old']['revision']==old_revision
    assert mapping['new']['id']!=rid
    assert mapping['new']['ciphertext_digest']!=digest(old_cipher)
    resolved=await call(app,'identity.custodial_migration_get',{
        'challenge_id':cid,'old_revision':old_revision},subject=subject,token=token)
    assert resolved.status=='ok',wire(resolved)
    assert resolved.data['source']==mapping['old'] and resolved.data['mapped']==mapping['new']
    async with app.metadata.transaction(write=False) as tx:
        original=await tx.resource(rid)
        assert original.revision==current_revision and original.generation==2
        assert (await tx.revision(ResourceRef(id=rid,revision=old_revision))).content.digest==digest(old_cipher)
        copied=await tx.revision(ResourceRef(id=mapping['new']['id'],
                                               revision=mapping['new']['revision']))
        copy_cipher=await app.contents.read_bytes(copied.content)
        assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(subject,))[0]=='active'
    identity_file=tmp_path/'new.agekey'
    identity_file.write_text(new_identity+'\n')
    identity_file.chmod(0o600)
    assert _age('--decrypt','--identity',str(identity_file),input_data=copy_cipher)==old_plain
    second=await call(app,'identity.custodial_rewrap_revision',{
        'challenge_id':cid,'ciphertext_ref':{'id':rid,'revision':current_revision},
        'old_encryption_key_id':created.data['encryption_key_id'],
        'new_recipient':new_recipient},subject=subject,token=token)
    assert second.status=='ok',wire(second)
    for index,item in enumerate((mapping,second.data['mapping'])):
        new=item['new']
        async with app.metadata.transaction(write=False) as tx:
            new_rev=await tx.revision(ResourceRef(id=new['id'],revision=new['revision']))
            encrypted=await app.contents.read_bytes(new_rev.content)
        assert _age('--decrypt','--identity',str(identity_file),input_data=encrypted)==(
            old_plain if index==0 else current_plain)
        request_id='ack-history-'+str(index)
        plain=old_plain if index==0 else current_plain
        signed={'subject_id':subject,'challenge_id':cid,'old':item['old'],
                'new':new,'plaintext_digest':digest(plain),'request_id':request_id}
        ack=signer.sign(canonical(signed),purpose='custodial-rewrap-ack')
        accepted=await call(app,'identity.custodial_rewrap_ack',{
            'challenge_id':cid,'old_revision':item['old']['revision'],
            'new_revision':new['revision'],
            'ciphertext_digest':new['ciphertext_digest'],
            'plaintext_digest':digest(plain),'decryption_ack':wire(ack)},
            subject=subject,token=token,rid=request_id)
        assert accepted.status=='ok',wire(accepted)
    coverage=await call(app,'identity.custodial_upgrade_inventory',
                        {'challenge_id':cid},subject=subject,token=token)
    assert coverage.status=='ok' and coverage.data['mapped_count']==2
    assert coverage.data['acked_count']==2
    assert coverage.data['known_ciphertexts_migrated'] is True
    assert coverage.data['finalize_ready'] is False
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind=='custodial'
        assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(subject,))[0]=='active'
    late_cipher=_age('--encrypt','--recipient',recipient,input_data=b'late write')
    await _trusted_age_entry(app,subject,late_cipher,suffix='after-migration')
    drifted=await call(app,'identity.custodial_upgrade_inventory',
                       {'challenge_id':cid},subject=subject,token=token)
    assert drifted.status=='ok' and drifted.data['inventory_changed']
    assert drifted.data['known_ciphertexts_migrated'] is False
    assert drifted.data['finalize_ready'] is False
