"""A custodian can recover ciphertext offline without gaining account authority."""
from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from msg.client import ClientState, MsgClient
from msg.client_recovery import (
    create_recovery_envelope, restore_recovery_envelope, save_recovery_envelope,
)
from msg.core.errors import Failure
from msg.security.age_keys import (
    encryption_key_id, generate_age_key, public_from_recipient,
    recipient_from_identity,
)
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from test_service import NOW


@pytest.mark.asyncio
async def test_age_multi_recipient_backup_is_opt_in_and_offline(installed,tmp_path):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app,_=installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        state=ClientState(tmp_path/'client',server=app.settings.service_url)
        client=MsgClient(state,HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
        await client.register('recovery-owner')
        assert not (await client.call('keystore.list',{})).data['items']
        first_secret,first=generate_age_key()
        second_secret,second=generate_age_key()
        third_secret,third=generate_age_key()
        for name,secret in (('first',first_secret),('second',second_secret),('third',third_secret)):
            path=tmp_path/(name+'.agekey')
            path.write_text(secret+'\n')
            path.chmod(0o600)
        with pytest.raises(Failure,match='independent_recovery_recipient_required'):
            create_recovery_envelope(state,(state.encryption_recipient,))
        key_id=encryption_key_id(public_from_recipient(state.encryption_recipient))
        policy=client.checked(await client.call('identity.recovery_policy_set',{
            'expected_version':0,'encryption_key_id':key_id,
            'recipients':[{'recipient':first},{'recipient':second}]}))
        assert policy.data['version']==1 and policy.data['opted_in']
        result,registered,metadata=await save_recovery_envelope(
            client,(first,second),policy_version=policy.data['version'])
        assert result.status==registered.status=='ok'
        assert registered.data['recipient_claim']=='owner_declared_unverified'
        assert metadata['subject_id']==state.subject
        assert metadata['encryption_key_id']==encryption_key_id(
            public_from_recipient(state.encryption_recipient))
        assert len(metadata['recipient_fingerprints'])==2
        entry=client.checked(await client.call('keystore.get',{'id':result.resources[0].id}))
        assert entry.data['format']=='age'
        ciphertext=tmp_path/'download.age'
        await client.download(entry.output,ciphertext)
        for name in ('first','second'):
            restored=tmp_path/(name+'-restored.agekey')
            proof=restore_recovery_envelope(ciphertext.read_bytes(),tmp_path/(name+'.agekey'),
                restored,expected_subject_id=state.subject,
                expected_encryption_key_id=metadata['encryption_key_id'])
            assert proof['account_authority']=='none'
            assert recipient_from_identity(restored.read_text().strip())==state.encryption_recipient
            assert restored.stat().st_mode&0o077==0
        with pytest.raises(Failure,match='age_operation_failed'):
            restore_recovery_envelope(ciphertext.read_bytes(),tmp_path/'third.agekey',
                tmp_path/'third-restored.agekey',expected_subject_id=state.subject,
                expected_encryption_key_id=metadata['encryption_key_id'])
        with pytest.raises(Failure,match='recovery_envelope_mismatch'):
            restore_recovery_envelope(ciphertext.read_bytes(),tmp_path/'first.agekey',
                tmp_path/'wrong-subject.agekey',expected_subject_id='u_wrong',
                expected_encryption_key_id=metadata['encryption_key_id'])
        assert not (tmp_path/'wrong-subject.agekey').exists()
        assert state.age_key_path.read_text().strip().encode() not in ciphertext.read_bytes()
        assert third!=first and third!=second
