"""Private-key-issued API keys reuse one-time token delivery and recovery journals."""

from datetime import timedelta

from msg.atomic_file import durable_write
from msg.client_tokens import remove_journal, token_operation
from msg.core.codec import canonical, loads, wire
from msg.core.errors import require


def accept(state, result):
    state.data['api_key'] = {
        'credential_id': result.data['credential_id'],
        'value': result.data['token'],
        'expires_at': result.data['expires_at'],
    }
    state._save()


@token_operation
async def create(client, *, ttl=86400, ceiling=None, rotate=False):
    require(client.state.signer is not None, 'signing_identity_required')
    old = client.state.data.get('api_key')
    require(not rotate or old is not None, 'api_key_required')
    path = client.state.directory / 'api-key-create.json'
    existing, pending = client._token_journal()
    intention = {
        'ttl': ttl,
        **({'ceiling': ceiling} if ceiling is not None else {}),
        **({'previous_credential': old['credential_id']} if rotate else {}),
    }
    if pending is None:
        pending = client._new_token_journal(path, 'identity.token_create', contract_version=3)
        pending['arguments'] = intention
        durable_write(path, canonical(pending), mode=0o600)
    else:
        require(
            existing == path and pending.get('arguments') == intention, 'token_operation_pending'
        )
    packet = client.prepare(
        'identity.token_create',
        dict(intention, nonce=pending['nonce'], recovery_secret=pending['recovery_secret']),
        signer=client.state.signer,
        request_id=pending['request_id'],
        contract_version=3,
        expires_at=client.clock() + timedelta(seconds=180),
    )
    result = await client._send_token_secret(packet)
    if result.status == 'ok':
        require(
            result.data['credential_id'] == pending['credential_id']
            and result.data['subject_id'] == pending['subject_id']
            and result.data.get('token'),
            'token_claim_mismatch',
        )
        accept(client.state, result)
        remove_journal(path)
    return result


async def run_command(client, args, arguments):
    if args.action == 'show':
        saved = client.state.data.get('api_key')
        return {k: v for k, v in (saved or {}).items() if k != 'value'}
    if args.action in {'create', 'rotate'}:
        ceiling = loads(args.ceiling.encode()) if args.ceiling else None
        require(ceiling is None or isinstance(ceiling, list), 'invalid_ceiling')
        result = await create(client, ttl=args.ttl, ceiling=ceiling, rotate=args.action == 'rotate')
        if result.status == 'ok':
            return {
                'api_key': result.data['credential_id'] + '.' + result.data['token'],
                'expires_at': result.data['expires_at'],
                'saved': True,
            }
        return result
    saved = client.state.data.get('api_key')
    require(saved is not None and client.state.signer is not None, 'signing_identity_required')
    result = await client.call(
        'identity.key_revoke', {'key_id': saved['credential_id']}, signer=client.state.signer
    )
    if result.status == 'ok':
        client.state.data.pop('api_key', None)
        client.state._save()
    return wire(result)
