"""Local, journaled custodial history recovery. Plaintext never leaves this client."""
from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

from msg.client_recovery import _age, _protected_file
from msg.client_secrets import write_private
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import Failure, require
from msg.security.age_keys import encryption_key_id, public_from_recipient, recipient_from_identity
from msg.security.custodial_migration import ack_statement, stage_statement
from msg.security.vault import client_upgrade_proof
from msg.storage.git import durable_write


def journal_for(state):
    for name in ('custodial-upgrade.json', 'custodial-history.json'):
        path = state.directory / name
        if path.exists() or path.is_symlink():
            path = _protected_file(path)
            require(path.stat().st_size <= 4 * 1024 * 1024, 'custodial_journal_too_large')
            body = loads(path.read_bytes())
            challenge = body.get('challenge')
            require(challenge and challenge['subject_id'] == state.subject and
                    challenge['public_key'] == body['public_key'] and
                    body['encryption_recipient'] == state.encryption_recipient and
                    body.get('server', state.server) == state.server,
                    'custodial_upgrade_journal_mismatch')
            require(state.signer is not None and body['public_key'] == wire(state.signer.public_key),
                    'custodial_upgrade_journal_mismatch')
            return path, body
    raise Failure('custodial_upgrade_journal_missing')


async def inventory(client):
    _, journal = journal_for(client.state)
    return client.checked(await client.call('identity.custodial_upgrade_inventory',
        {'challenge_id': journal['challenge']['challenge_id']}))


async def transition(client, action, *, resolution='verified', loss_revisions=(), reason=None,
                     external_ciphertexts_migrated=False):
    from msg.client_upgrade import locked_state
    from msg.client_tokens import remove_journal
    client._require_token_secret_transport()
    with locked_state(client.state):
        path, journal = journal_for(client.state)
        challenge = journal['challenge']
        pending = journal.get('pending_transition')
        intent = {'action': action, 'resolution': resolution, 'loss_revisions': sorted(loss_revisions),
                  'reason': reason, 'external_ciphertexts_migrated': external_ciphertexts_migrated}
        if pending is None:
            state = (await inventory(client)).data
            identity = _protected_file(client.state.age_key_path).read_text().strip()
            require(recipient_from_identity(identity) == state['recipient'], 'encryption_identity_mismatch')
            args = {'challenge_id': challenge['challenge_id'], 'action': action,
                'age_proof': client_upgrade_proof(identity, challenge),
                'external_ciphertexts_migrated': external_ciphertexts_migrated,
                'inventory_digest': state['inventory_digest'], 'observed_digest': state['observed_digest'],
                'results_digest': state['results_digest']}
            if action == 'finalize':
                args.update(resolution=resolution, loss_revisions=sorted(loss_revisions),
                            reviewed_policy_version=state['recovery_policy_version'])
                if reason is not None:
                    args['reason'] = reason
            request_id = uuid4().hex
            args['migration_ack'] = wire(client.state.signer.sign(canonical(
                stage_statement(client.state.subject, args, request_id)), purpose='custodial-decision-v1'))
            pending = {'intent': intent, 'arguments': args, 'request_id': request_id}
            journal['pending_transition'] = pending
            journal['server'] = client.state.server
            durable_write(path, canonical(journal), mode=0o600)
        else:
            require(pending['intent'] == intent, 'custodial_transition_pending')
        async def recovered():
            result = await client.call('identity.custodial_upgrade_result',
                {'challenge_id': challenge['challenge_id']}, signer=client.state.signer,
                subject=client.state.subject, certificates=())
            require(result.status == 'ok' and result.data.get('finalization_request_id') ==
                    pending['request_id'], 'custodial_transition_recovery_pending')
            return result
        try:
            result = await client.call('identity.custodial_upgrade_finish', pending['arguments'],
                                       request_id=pending['request_id'])
        except Failure as exc:
            if exc.code != 'transport_uncertain' or action != 'finalize':
                raise
            result = await recovered()
        if result.status == 'error' and action == 'finalize' and result.error.code in {
                'credential_revoked', 'credential_expired', 'custodial_token_required'}:
            result = await recovered()
        if result.status == 'ok':
            journal.pop('pending_transition', None)
            if action == 'finalize':
                client.state.accept_identity(result)
                journal['status'] = 'completed'
                journal['completed_result'] = wire(result.data)
                destination = client.state.directory / 'custodial-history.json'
                durable_write(destination, canonical(journal), mode=0o600)
                if path != destination:
                    remove_journal(path)
            else:
                durable_write(path, canonical(journal), mode=0o600)
        elif result.error.code not in {'internal_error', 'transport_uncertain'}:
            # A signed refusal proves this decision did not commit. A fresh
            # observation may be taken; uncertain transport keeps the journal.
            journal.pop('pending_transition', None)
            durable_write(path, canonical(journal), mode=0o600)
        return result


async def _ciphertext(client, ref, directory, name):
    entry = client.checked(await client.call('keystore.get', {'id': ref['id'], 'revision': ref['revision']}))
    require(entry.data['format'] == 'age' and entry.data['size'] <= 1114112,
            'age_keystore_entry_required')
    path = directory / name
    await client.download(entry.output, path)
    value = path.read_bytes()
    require(digest(value) == ref['ciphertext_digest'], 'custodial_rewrap_ciphertext_mismatch')
    return value


async def acknowledge(client, old_revision, *, method='rewrap'):
    from msg.client_upgrade import locked_state
    with locked_state(client.state):
        state = (await inventory(client)).data
        require(not state['inventory_changed'], 'custodial_inventory_changed')
        source = next((item for item in state['sources'] if item['revision'] == old_revision), None)
        require(source is not None, 'custodial_rewrap_not_in_frozen_inventory')
        identity_path = _protected_file(client.state.age_key_path)
        require(recipient_from_identity(identity_path.read_text().strip()) == state['recipient'],
                'custodial_rewrap_recipient_mismatch')
        with tempfile.TemporaryDirectory(prefix='custodial-history-', dir=client.state.directory) as temp:
            directory = Path(temp)
            if method == 'rewrap':
                mapping = state['mappings'].get(old_revision)
                require(mapping is not None and mapping['old'] == {k: v for k, v in source.items() if k != 'key_id'},
                        'custodial_rewrap_mapping_missing')
                target = dict(mapping['new'], key_id=state['new_key_id'], recipient=state['recipient'])
                ciphertext = await _ciphertext(client, mapping['new'], directory, 'mapped.age')
                plaintext = _age('--decrypt', '--identity', str(identity_path), input_data=ciphertext)
                target_ref = mapping['new']
            else:
                require(method == 'compatibility_recovery', 'invalid_recovery_method')
                target = state['recovery_envelope']
                require(target is not None and target['subject_id'] == client.state.subject and
                        target['old_key_id'] == source['key_id'] and target['new_key_id'] == state['new_key_id'] and
                        target['recipient'] == state['recipient'], 'custodial_recovery_envelope_mismatch')
                encrypted_key = await _ciphertext(client, target['ciphertext'], directory, 'key.age')
                raw = _age('--decrypt', '--identity', str(identity_path), input_data=encrypted_key)
                payload = loads(raw)
                require(set(payload) == {'format','purpose','subject_id','challenge_id','encryption_key_id',
                                         'encryption_recipient','age_identity'} and
                        payload['format'] == 'msg-custodial-retired-key-v1' and
                        payload['purpose'] == 'retired-encryption-subkey-recovery' and
                        payload['subject_id'] == client.state.subject and
                        payload['challenge_id'] == state['challenge_id'] and
                        payload['encryption_key_id'] == source['key_id'], 'custodial_recovery_envelope_mismatch')
                old_recipient = recipient_from_identity(payload['age_identity'])
                require(old_recipient == payload['encryption_recipient'] and
                        encryption_key_id(public_from_recipient(old_recipient)) == source['key_id'],
                        'custodial_recovery_key_mismatch')
                old_path = directory / 'retired.agekey'
                write_private(old_path, (payload['age_identity'] + '\n').encode('ascii'))
                ciphertext = await _ciphertext(client, source, directory, 'original.age')
                plaintext = _age('--decrypt', '--identity', str(old_path), input_data=ciphertext)
                target_ref = target['ciphertext']
            request_id = uuid4().hex
            plaintext_digest = digest(plaintext)
            signed = ack_statement(client.state.subject, state['challenge_id'], state['inventory_digest'],
                                   source, target, plaintext_digest, request_id, method=method)
            signature = client.state.signer.sign(canonical(signed), purpose='custodial-history-ack-v1')
        return client.checked(await client.call('identity.custodial_rewrap_ack', {
            'challenge_id': state['challenge_id'], 'old_revision': old_revision,
            'new_revision': target_ref['revision'], 'ciphertext_digest': target_ref['ciphertext_digest'],
            'plaintext_digest': plaintext_digest, 'inventory_digest': state['inventory_digest'],
            'method': method, 'decryption_ack': wire(signature)}, request_id=request_id))


async def migrate(client, *, method='rewrap', limit=100):
    require(type(limit) is int and 1 <= limit <= 1000, 'invalid_migration_limit')
    state = (await inventory(client)).data
    require(not state['inventory_changed'], 'custodial_inventory_changed')
    if method == 'compatibility_recovery' and state['recovery_envelope'] is None:
        client.checked(await transition(client, 'recovery_envelope'))
    outcomes = []
    for source in state['sources']:
        revision = source['revision']
        if revision in state['verified_revisions']:
            continue
        if len(outcomes) == limit:
            break
        try:
            chosen = 'rewrap' if source['key_id'] == state['new_key_id'] else method
            if chosen == 'rewrap' and revision not in state['mappings']:
                client.checked(await client.call('identity.custodial_rewrap_revision', {
                    'challenge_id': state['challenge_id'],
                    'ciphertext_ref': {'id': source['id'], 'revision': revision},
                    'old_encryption_key_id': state['old_key_id'], 'new_recipient': state['recipient']}))
            result = await acknowledge(client, revision, method=chosen)
            outcomes.append({'revision': revision, 'status': result.status, 'method': chosen})
        except Failure as exc:
            outcomes.append({'revision': revision, 'status': 'pending', 'error': exc.code})
    return {'items': outcomes, 'inventory': wire((await inventory(client)).data), 'finalized': False}
