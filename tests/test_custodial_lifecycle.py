"""Issue #68: real age round trips, scoped decisions, deltas and deletion boundaries."""
from dataclasses import replace
import os
from uuid import uuid4

import httpx
import pytest

from msg.client import ClientState, MsgClient
from msg.client_custodial import inventory, transition, migrate, acknowledge
from msg.client_recovery import _age
from msg.core.codec import b64, canonical, digest, loads, unb64, wire
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer
from msg.security.vault import open_signer, seal_retired_encryption_key
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from test_custodial_rewrap import _trusted_age_entry
from test_custodial_upgrade import begin, finish
from test_service import NOW, call, register


async def client_with_history(app, directory, *, alien=False):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                            base_url=app.settings.service_url)
    state = ClientState(directory, server=app.settings.service_url)
    client = MsgClient(state, HTTPTransport(app.settings.service_url, http=http), clock=lambda: NOW)
    created = client.checked(await client.custodial('lifecycle-' + uuid4().hex[:8]))
    old_token = state.token
    old_recipient = created.data['encryption_recipient']
    recipient = generate_age_key()[1] if alien else old_recipient
    ciphertext = _age('--encrypt', '--recipient', recipient, input_data=b'original private bytes')
    rid, revision = await _trusted_age_entry(app, state.subject, ciphertext)
    pending = await client.upgrade_custodial('self-' + uuid4().hex[:8], external_ciphertexts_migrated=True)
    assert pending.status == 'ok' and pending.data['status'] == 'pending_rewrap', wire(pending)
    return client, http, old_token, created, (rid, revision, ciphertext)


@pytest.mark.asyncio
async def test_all_mapped_history_switches_but_never_claims_backup_retirement(installed, tmp_path):
    app, _ = installed
    client, http, token, created, source = await client_with_history(app, tmp_path/'client')
    try:
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT recipient FROM encryption_subkeys WHERE subject=? AND is_primary=1',
                          (client.state.subject,)) == (client.state.encryption_recipient,)
        migrated = await migrate(client)
        assert migrated['items'][0]['status'] == 'ok'
        assert migrated['inventory']['history_recoverable'] is True
        completed = await transition(client, 'finalize', external_ciphertexts_migrated=True)
        assert completed.status == 'ok', wire(completed)
        assert client.state.token is None
        assert completed.data['phase'] == 'online_key_retired'
        assert completed.data['completion_scope'] == 'identity_switch_only'
        assert completed.data['history_recoverable'] is True
        assert completed.data['retirement']['online_signing_key_deleted']
        assert completed.data['retirement']['online_encryption_key_deleted']
        assert not completed.data['retirement']['backup_retired']
        assert not completed.data['retirement']['server_key_retired']
        assert completed.data['external_ciphertexts_verified'] is False
        assert (await inventory(client)).data['history_recoverable'] is True
        rejected = await call(app, 'content.post_create', {'parent':'/main','body':'old token'},
                              subject=client.state.subject, token=token)
        assert rejected.status == 'error'
        async with app.metadata.transaction(write=False) as tx:
            resource = await tx.resource(source[0])
            assert resource.revision == source[1] and resource.generation == 1
            rev = await tx.revision(ResourceRef(id=source[0], revision=source[1]))
            assert await app.contents.read_bytes(rev.content) == source[2]
            assert tx.one('SELECT status,signing_ciphertext,age_ciphertext FROM custodial_vault WHERE subject=?',
                          (client.state.subject,)) == ('destroyed',None,None)
            assert tx.one("SELECT COUNT(*) FROM audit WHERE body LIKE '%custodial_tokens_revoked%'")[0] == 1
            assert tx.one("SELECT COUNT(*) FROM audit WHERE body LIKE '%custodial_online_key_retirement%'")[0] == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_compatibility_envelope_contains_only_owned_retired_age_key(installed,tmp_path):
    app,_=installed
    client,http,token,created,source=await client_with_history(app,tmp_path/'client')
    try:
        envelope_result=await transition(client,'recovery_envelope')
        assert envelope_result.status=='ok',wire(envelope_result)
        envelope=envelope_result.data['recovery_envelope']
        assert envelope['method']=='compatibility_recovery'
        async with app.metadata.transaction(write=False) as tx:
            ref=envelope['ciphertext']
            resource=await tx.resource(ref['id'])
            assert resource.mode==0o600 and resource.owner==client.state.subject
            ciphertext=await app.contents.read_bytes((await tx.revision(
                ResourceRef(id=ref['id'],revision=ref['revision']))).content)
            with pytest.raises(Failure,match='custodial_recovery_requires_retired_owned_key'):
                seal_retired_encryption_key(app,tx,'u_other',envelope['old_key_id'],
                    client.state.encryption_recipient,envelope['challenge_id'])
        raw=_age('--decrypt','--identity',str(client.state.age_key_path),input_data=ciphertext)
        payload=loads(raw)
        assert set(payload)=={'format','purpose','subject_id','challenge_id','encryption_key_id',
                              'encryption_recipient','age_identity'}
        assert payload['encryption_key_id']==created.data['encryption_key_id']
        assert payload['encryption_recipient']==created.data['encryption_recipient']
        outsider,uid,_=await register(app,'envelope-outsider')
        rejected=await call(app,'keystore.get',{'id':ref['id']},subject=uid,key=outsider)
        assert rejected.status=='error'
        migrated=await migrate(client,method='compatibility_recovery')
        assert migrated['inventory']['history_recoverable'] is True
        assert not migrated['inventory']['mappings']  # B is never mislabelled re-encryption.
        assert migrated['inventory']['methods'][source[1]]=='compatibility_recovery'
        done=await transition(client,'finalize',external_ciphertexts_migrated=True)
        assert done.status=='ok' and done.data['history_recoverable']
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_new_write_delta_requires_refresh_and_all_acks_are_rebound(installed,tmp_path):
    app,_=installed
    client,http,token,created,source=await client_with_history(app,tmp_path/'client')
    try:
        first=await migrate(client)
        first_digest=first['inventory']['inventory_digest']
        async with app.metadata.transaction(write=False) as tx:
            signing=open_signer(app,tx,client.state.subject)
        async with app.metadata.transaction(write=True) as tx:
            credential=await tx.credential(signing.key_id)
            subject=await tx.subject(client.state.subject)
            await tx.save_credential(replace(credential,ceiling=app.primary_ceiling()),subject.auth_version)
        ciphertext=_age('--encrypt','--recipient',client.state.encryption_recipient,input_data=b'new-key write')
        args={'name':'during.age','format':'age','ciphertext':b64(ciphertext)}
        missing=await call(app,'keystore.put',args,subject=client.state.subject,key=signing,contract_version=2)
        assert missing.status=='error' and missing.error.code=='custodial_new_encryption_key_required'
        wrong=await call(app,'keystore.put',dict(args,encryption_key_id=created.data['encryption_key_id']),
                         subject=client.state.subject,key=signing,contract_version=2)
        assert wrong.status=='error' and wrong.error.code=='custodial_new_encryption_key_required'
        latest=await call(app,'keystore.put',dict(args,encryption_key_id=first['inventory']['new_key_id']),
                          subject=client.state.subject,key=signing,contract_version=2)
        assert latest.status=='ok',wire(latest)
        drift=(await inventory(client)).data
        assert drift['inventory_changed'] and len(drift['delta']['added'])==1
        denied=await transition(client,'finalize',external_ciphertexts_migrated=True)
        assert denied.status=='error' and denied.error.code=='custodial_inventory_changed'
        refreshed=await transition(client,'refresh')
        assert refreshed.status=='ok',wire(refreshed)
        assert refreshed.data['inventory_digest']!=first_digest
        assert not refreshed.data['verified_revisions']
        assert len(refreshed.data['unresolved_revisions'])==2
        again=await migrate(client)
        assert again['inventory']['history_recoverable'] is True
        assert len(again['inventory']['verified_revisions'])==2
        assert (await transition(client,'finalize',external_ciphertexts_migrated=True)).status=='ok'
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_unmigratable_requires_exact_signed_loss_decision(installed,tmp_path):
    app,_=installed
    client,http,token,created,source=await client_with_history(app,tmp_path/'client',alien=True)
    try:
        attempted=await migrate(client)
        assert attempted['items']==[{'revision':source[1],'status':'pending','error':'custodial_rewrap_decrypt_failed'}]
        failed=await transition(client,'finalize',external_ciphertexts_migrated=True)
        assert failed.status=='error' and failed.error.code=='custodial_history_ack_required'
        assert client.state.token is not None
        wrong=await transition(client,'finalize',resolution='accept_loss',loss_revisions=('v_other',),
                               reason='I accept this listed loss',external_ciphertexts_migrated=True)
        assert wrong.status=='error' and wrong.error.code=='custodial_loss_scope_mismatch'
        done=await transition(client,'finalize',resolution='accept_loss',loss_revisions=(source[1],),
                              reason='Explicitly accept this unrecoverable revision',external_ciphertexts_migrated=True)
        assert done.status=='ok',wire(done)
        assert done.data['history_recoverable'] is False
        assert done.data['resolution']['revisions']==(source[1],)
        assert done.data['retirement']['server_key_retired'] is False
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_decrypt_only_retention_cannot_sign_and_can_finish_later(installed,tmp_path):
    app,_=installed
    client,http,token,created,source=await client_with_history(app,tmp_path/'client')
    try:
        retained=await transition(client,'finalize',resolution='retain_decrypt',loss_revisions=(source[1],),
                                  reason='Keep only recovery decryption until this revision is verified')
        client.checked(retained)
        assert retained.status=='ok'
        assert client.state.token is None
        assert retained.data['phase']=='identity_switched'
        assert retained.data['retirement']['online_signing_key_deleted'] is True
        assert retained.data['retirement']['online_encryption_key_deleted'] is False
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT status FROM custodial_vault WHERE subject=?',(client.state.subject,))==('decrypt_only',)
            with pytest.raises(Failure,match='custodial_vault_unavailable'):
                open_signer(app,tx,client.state.subject)
        migrated=await migrate(client)
        assert migrated['inventory']['history_recoverable'] is True
        done=await transition(client,'finalize',external_ciphertexts_migrated=True)
        assert done.status=='ok' and done.data['retirement']['online_encryption_key_deleted']
        assert not done.data['retirement']['backup_retired']
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_committed_finalize_response_loss_recovers_exact_result(installed,tmp_path,monkeypatch):
    app,_=installed
    client,http,token,created,source=await client_with_history(app,tmp_path/'client')
    try:
        await migrate(client)
        original=client.transport.call
        lost=False
        async def send(packet):
            nonlocal lost
            result=await original(packet)
            if packet.operation=='identity.custodial_upgrade_finish' and packet.arguments.get('action')=='finalize' and not lost:
                lost=True
                assert result.status=='ok',wire(result)
                raise Failure('transport_uncertain')
            return result
        monkeypatch.setattr(client.transport,'call',send)
        done=await transition(client,'finalize',external_ciphertexts_migrated=True)
        assert lost and done.status=='ok' and client.state.token is None
        assert not (client.state.directory/'custodial-upgrade.json').exists()
        saved=loads((client.state.directory/'custodial-history.json').read_bytes())
        assert 'pending_transition' not in saved
        assert saved['completed_result']['finalization_request_id']==done.data['finalization_request_id']
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM audit WHERE body LIKE '%custodial_tokens_revoked%'")[0]==1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_duplicate_start_and_rotation_cannot_replace_pending_target(installed, tmp_path):
    app, _ = installed
    client, http, token, created, source = await client_with_history(app, tmp_path/'client')
    try:
        other_key = Ed25519Signer.generate()
        other_identity, other_recipient = generate_age_key()
        duplicate = await begin(app, client.state.subject, token, other_key, other_recipient)
        assert duplicate.status == 'error' and duplicate.error.code == 'custodial_upgrade_already_pending'
        retained = await transition(client, 'finalize', resolution='retain_decrypt',
            loss_revisions=(source[1],), reason='Finish decryption later')
        assert retained.status == 'ok'
        blocked = await client.call('identity.encryption_key_rotate', {'encryption_recipient': other_recipient})
        assert blocked.status == 'error' and blocked.error.code == 'custodial_migration_pending'
        assert (await inventory(client)).data['recipient'] == client.state.encryption_recipient
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_scoped_ack_and_decision_tampering_never_retire_keys(installed, tmp_path):
    from msg.security.custodial_migration import ack_statement, stage_statement
    from msg.security.vault import client_upgrade_proof
    from msg.client_custodial import journal_for
    app, _ = installed
    client, http, token, created, source = await client_with_history(app, tmp_path/'client')
    try:
        view = (await inventory(client)).data
        made = client.checked(await client.call('identity.custodial_rewrap_revision', {
            'challenge_id': view['challenge_id'], 'ciphertext_ref': {'id': source[0], 'revision': source[1]},
            'old_encryption_key_id': view['old_key_id'], 'new_recipient': view['recipient']}))
        view = (await inventory(client)).data
        mapping = made.data['mapping']
        target = dict(mapping['new'], key_id=view['new_key_id'], recipient=view['recipient'])
        rid = uuid4().hex
        statement = ack_statement(client.state.subject, view['challenge_id'], view['inventory_digest'],
                                  view['sources'][0], target, digest(b'original private bytes'), rid)
        args = {'challenge_id': view['challenge_id'], 'old_revision': source[1],
            'new_revision': target['revision'], 'ciphertext_digest': target['ciphertext_digest'],
            'plaintext_digest': statement['plaintext_digest'], 'inventory_digest': view['inventory_digest'],
            'decryption_ack': wire(client.state.signer.sign(canonical(statement), purpose='custodial-history-ack-v1'))}
        for field, value in (('inventory_digest', digest('stale')), ('plaintext_digest', digest('tamper')),
                             ('new_revision', source[1])):
            denied = await client.call('identity.custodial_rewrap_ack', dict(args, **{field: value}), request_id=rid,contract_version=2)
            assert denied.status == 'error'
        assert not (await inventory(client)).data['verified_revisions']
        await acknowledge(client, source[1])
        view = (await inventory(client)).data
        _, journal = journal_for(client.state)
        args = {'action': 'finalize', 'challenge_id': view['challenge_id'], 'resolution': 'verified',
            'age_proof': client_upgrade_proof(client.state.age_key_path.read_text().strip(), journal['challenge']),
            'external_ciphertexts_migrated': True, 'inventory_digest': view['inventory_digest'],
            'observed_digest': view['observed_digest'], 'results_digest': view['results_digest'],
            'reviewed_policy_version': view['recovery_policy_version'], 'loss_revisions': []}
        rid = uuid4().hex
        args['migration_ack'] = wire(client.state.signer.sign(canonical(stage_statement(
            client.state.subject, args, rid)), purpose='custodial-decision-v1'))
        args['resolution'] = 'accept_loss'
        denied = await client.call('identity.custodial_upgrade_finish', args, request_id=rid,contract_version=2)
        assert denied.status == 'error' and denied.error.code == 'invalid_signature'
        assert client.state.token == token
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT status FROM custodial_vault WHERE subject=?', (client.state.subject,)) == ('active',)
    finally:
        await http.aclose()
