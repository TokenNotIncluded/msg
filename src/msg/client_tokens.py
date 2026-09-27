"""Durable, single-owner token issuance and bounded lineage recovery.

A request proof can be refreshed. Its business request ID/input and the server's
original credential expiry/recovery deadline cannot. Secret material is local
0600 journal data, never a URL, status field, or exception message.
"""
import hashlib
import os
import re
from datetime import timedelta
from uuid import uuid4

from msg.client_journal import identity_lock, read_owned_json, remove_journal
from msg.core.codec import b64, canonical, digest, unb64, wire
from msg.core.errors import Failure, require
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION, request_for
from msg.security.crypto import Ed25519Signer
from msg.storage.git import durable_write

JOURNALS = {
    'identity.temporary': 'temporary.json',
    'identity.custodial_create': 'custodial-bootstrap.json',
    'identity.token_rotate': 'token-rotation.json',
    'identity.token_create': 'token-create.json',
}
BOOTSTRAP = frozenset({'identity.temporary', 'identity.custodial_create'})


def claim(nonce, request_id, operation, subject=None):
    if operation in BOOTSTRAP:
        prefix = 'u_tmp_' if operation == 'identity.temporary' else 'u_cust_'
        subject = prefix + hashlib.sha256(unb64(nonce, limit=64)).hexdigest()[:32]
        credential = 't_' + subject[2:]
    else:
        require(subject is not None, 'token_subject_required')
        credential = 't_' + digest((request_id, subject))[7:39]
    return subject, credential


def read_journal(state):
    paths = [state.directory/name for name in JOURNALS.values()]
    present = [path for path in paths if path.exists() or path.is_symlink()]
    require(len(present) <= 1, 'multiple_token_journals')
    if not present:
        return None, None
    path = present[0]
    pending = read_owned_json(path, maximum=65536, unsafe='unsafe_token_journal',
                              invalid='invalid_token_journal')
    try:
        operation = pending['operation']
        require(operation in JOURNALS and JOURNALS[operation] == path.name, 'invalid_token_journal')
        require(pending['contract_version'] == SECRET_DELIVERY_MIN_VERSION[operation],
                'legacy_token_journal_requires_manual_resolution')
        require(pending.get('server', state.server) == state.server, 'token_journal_mismatch')
        for field in ('nonce', 'recovery_secret'):
            require(len(unb64(pending[field], limit=64)) >= 32, 'invalid_token_journal')
        require(pending['nonce'] != pending['recovery_secret'], 'invalid_token_journal')
        require(re.fullmatch('[0-9a-f]{32}', pending['request_id']) is not None,
                'invalid_token_journal')
        generation = pending.get('generation', 0)
        # Earlier clients advanced the ID/material but did not write a generation.
        # A bootstrap descendant is identifiable without changing any secret or
        # trusting an arbitrary new subject: verify its original nonce-bound ID.
        if 'generation' not in pending and operation in BOOTSTRAP:
            initial_subject, initial_id = claim(pending['nonce'], pending['request_id'], operation)
            require(initial_subject == pending['subject_id'], 'token_journal_mismatch')
            if pending['credential_id'] != initial_id:
                generation = 1
                pending['generation'] = generation
        require(type(generation) is int and generation >= 0, 'invalid_token_journal')
        subject, credential = claim(pending['nonce'], pending['request_id'],
            'identity.token_recover' if generation else operation, pending['subject_id'])
        require((subject, credential) == (pending['subject_id'], pending['credential_id']),
                'token_journal_mismatch')
        require(state.subject is None and operation in BOOTSTRAP or state.subject == subject,
                'token_journal_mismatch')
        if operation == 'identity.temporary':
            require(state.signer is not None and state.encryption_recipient is not None,
                    'token_journal_key_missing')
        if 'public_key' in pending:
            require(state.signer is not None and b64(state.signer.public_key) == pending['public_key'],
                    'token_journal_mismatch')
        if 'encryption_recipient' in pending:
            require(state.encryption_recipient == pending['encryption_recipient'], 'token_journal_mismatch')
        recovery = pending.get('recovery')
        if recovery is not None:
            require(isinstance(recovery, dict) and
                    re.fullmatch('[0-9a-f]{32}', recovery['request_id']) is not None,
                    'invalid_token_journal')
            require(len(unb64(recovery['nonce'], limit=64)) >= 32 and
                    len(unb64(recovery['new_recovery_secret'], limit=64)) >= 32 and
                    recovery['new_recovery_secret'] not in {pending['recovery_secret'], recovery['nonce']},
                    'invalid_token_journal')
            require(claim(recovery['nonce'], recovery['request_id'], 'identity.token_recover', subject)[1]
                    == recovery['credential_id'], 'token_journal_mismatch')
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, Failure):
            raise
        raise Failure('invalid_token_journal') from None
    return path, pending


def _reload(state):
    # Do not keep an old in-memory token after another process completed a transition.
    fresh = type(state)(state.directory, server=state.server)
    state.data, state.signer = fresh.data, fresh.signer
    state.encryption_recipient = fresh.encryption_recipient


def pending_delivery(state):
    _, pending = read_journal(state)
    if pending is None:
        return None
    return {'operation': pending['operation'], 'request_id': pending['request_id'],
            'status': 'pending', 'resume': 'msg identity recover-token'}


def _new(client, operation, arguments):
    state = client.state
    require(not any((state.directory/name).exists() or (state.directory/name).is_symlink()
        for name in ('identity-upgrade.json', 'custodial-upgrade.json', 'registration.json')),
        'identity_recovery_pending')
    nonce, recovery_secret, request_id = b64(os.urandom(32)), b64(os.urandom(32)), uuid4().hex
    subject, credential = claim(nonce, request_id, operation, state.subject)
    pending = {'contract_version': SECRET_DELIVERY_MIN_VERSION[operation], 'operation': operation,
        'server': state.server, 'nonce': nonce, 'recovery_secret': recovery_secret,
        'request_id': request_id, 'subject_id': subject, 'credential_id': credential,
        'generation': 0, 'input': arguments}
    if state.signer is not None:
        pending['public_key'] = b64(state.signer.public_key)
    if operation == 'identity.temporary':
        pending['encryption_recipient'] = state.encryption_recipient
    if operation == 'identity.token_rotate':
        pending['authorizing_credential_id'] = state.token[0]
    path = state.directory/JOURNALS[operation]
    raw = canonical(pending)
    require(len(raw) <= 65536, 'token_journal_too_large')
    durable_write(path, raw, mode=0o600)
    return path, pending


def _credential_path(state, credential):
    # IDs were validated against the deterministic claim; never accept a server path.
    require(re.fullmatch(r't_[a-z0-9_]+', credential) is not None, 'token_claim_mismatch')
    return state.directory/('credential-' + credential + '.json')


def _accepted(state, pending, credential):
    if pending['operation'] != 'identity.token_create':
        return state.token is not None and state.token[0] == credential
    path = _credential_path(state, credential)
    if not (path.exists() or path.is_symlink()):
        return False
    value = read_owned_json(path, maximum=65536, unsafe='unsafe_client_credential',
                             invalid='invalid_client_credential')
    return (value.get('server') == state.server and value.get('subject_id') == pending['subject_id']
            and value.get('credential_id') == credential and bool(value.get('token')))


def _complete_locally(state, path, pending):
    candidates = [pending['credential_id']]
    if 'recovery' in pending:
        candidates.append(pending['recovery']['credential_id'])
    if any(_accepted(state, pending, credential) for credential in candidates):
        remove_journal(path)
        raise Failure('token_recovery_not_pending')


def _accept(state, path, pending, result, expected):
    require(result.status == 'ok' and result.subject == pending['subject_id'] and
            result.request_id == expected['request_id'] and
            result.data.get('credential_id') == expected['credential_id'] and
            result.data.get('subject_id') == pending['subject_id'] and
            bool(result.data.get('token')), 'token_claim_mismatch')
    if pending['operation'] == 'identity.token_create':
        # Minting a restricted credential does not silently replace the signing identity.
        durable_write(_credential_path(state, expected['credential_id']), canonical({
            'server': state.server, **result.data, 'receipt': wire(result.receipt),
            'request_id': result.request_id}), mode=0o600)
    else:
        state.accept_identity(result)
    remove_journal(path)
    return result


async def issue_token(client, operation, arguments=None):
    arguments = arguments or {}
    client._require_token_secret_transport()
    state = client.state
    with identity_lock(state.directory, busy='token_operation_busy', unsafe='unsafe_token_lock'):
        _reload(state)
        path, pending = read_journal(state)
        if pending is not None:
            _complete_locally(state, path, pending)
            require(pending['operation'] == operation, 'token_operation_pending')
            # A successor request is not a fresh bootstrap/rotation intention.
            require(not pending.get('generation', 0) and not pending.get('recovery'),
                    'token_recovery_in_progress')
            original_input = pending.get('input', {'handle': pending['handle']} if 'handle' in pending else {})
            require(canonical(original_input) == canonical(arguments), 'token_operation_pending')
        else:
            if operation in BOOTSTRAP:
                require(state.subject is None, 'identity_already_configured')
            if operation == 'identity.temporary':
                if state.signer is None:
                    state.save_signer(Ed25519Signer.generate())
                state.ensure_encryption_key()
            elif operation == 'identity.custodial_create':
                require(state.signer is None and state.encryption_recipient is None,
                        'identity_already_configured')
            elif operation == 'identity.token_rotate':
                require(state.token is not None, 'token_required')
            elif operation == 'identity.token_create':
                require(state.subject is not None and state.signer is not None and state.token is None,
                        'signing_identity_required')
            path, pending = _new(client, operation, arguments)
        if operation == 'identity.token_rotate':
            require(state.token is not None and state.token[0] ==
                    pending.get('authorizing_credential_id', state.token[0]), 'token_journal_mismatch')
        args = {**arguments, 'nonce': pending['nonce'], 'recovery_secret': pending['recovery_secret']}
        if operation == 'identity.temporary':
            args.update(public_key=b64(state.signer.public_key), encryption_recipient=state.encryption_recipient)
            proof = state.signer.sign(canonical({'subject_id': pending['subject_id'],
                'nonce': pending['nonce'], 'public_key': args['public_key'],
                'encryption_recipient': args['encryption_recipient'], 'request_id': pending['request_id']}),
                purpose='temporary-key-possession-v1')
            args['possession_proof'] = wire(proof)
        # Only short request expiry changes on restart. The digest excludes it;
        # replay cannot renew a credential or its committed delivery deadline.
        packet = client.prepare(operation, args, anonymous=operation in BOOTSTRAP,
            request_id=pending['request_id'], contract_version=pending['contract_version'])
        result = await client._send_token_secret(packet)
        return _accept(state, path, pending, result, pending) if result.status == 'ok' else result


async def recover_token(client):
    client._require_token_secret_transport()
    state = client.state
    with identity_lock(state.directory, busy='token_operation_busy', unsafe='unsafe_token_lock'):
        _reload(state)
        path, pending = read_journal(state)
        require(path is not None, 'token_recovery_not_pending')
        _complete_locally(state, path, pending)
        recovery = pending.get('recovery')
        if recovery is None:
            nonce, request_id = b64(os.urandom(32)), uuid4().hex
            recovery = {'nonce': nonce, 'new_recovery_secret': b64(os.urandom(32)),
                'request_id': request_id,
                'credential_id': claim(nonce, request_id, 'identity.token_recover', pending['subject_id'])[1]}
            pending['recovery'] = recovery
            durable_write(path, canonical(pending), mode=0o600)
        args = {'credential_id': pending['credential_id'], 'original_request_id': pending['request_id'],
            'recovery_secret': pending['recovery_secret'], 'nonce': recovery['nonce'],
            'new_recovery_secret': recovery['new_recovery_secret']}
        packet = request_for('identity.token_recover', args, state.server, subject=pending['subject_id'],
            request_id=recovery['request_id'], expires_at=client.clock()+timedelta(seconds=180), source='msg')
        result = await client._send_token_secret(packet)
        if result.status == 'ok':
            require(result.data.get('previous_credential') == pending['credential_id'], 'token_claim_mismatch')
            return _accept(state, path, pending, result, recovery)
        if result.error.code == 'token_delivery_unavailable':
            committed = (result.data or {}).get('committed_result', {})
            facts = committed.get('data', {})
            require(committed.get('operation') == 'identity.token_recover' and
                committed.get('request_id') == recovery['request_id'] and
                committed.get('subject') == pending['subject_id'] and
                facts.get('subject_id') == pending['subject_id'] and
                facts.get('credential_id') == recovery['credential_id'] and
                facts.get('previous_credential') == pending['credential_id'], 'token_claim_mismatch')
            pending.update(credential_id=recovery['credential_id'], request_id=recovery['request_id'],
                recovery_secret=recovery['new_recovery_secret'], generation=pending.get('generation', 0)+1)
            pending.pop('recovery')
            durable_write(path, canonical(pending), mode=0o600)
        return result
