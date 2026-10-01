"""Exchange public task-key requests; private keys stay on the worker."""

from pathlib import Path
from uuid import uuid4

from msg.atomic_file import durable_write
from msg.core.codec import b64, canonical, loads, parse_time, wire
from msg.core.errors import require
from msg.plugins.delegated_identity import possession_body
from msg.security.crypto import Ed25519Signer, subject_id


def read_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(1048577)
    require(len(raw) <= 1048576, 'delegation_file_too_large')
    return loads(raw)


def add_commands(identity):
    prepare = identity.add_parser('delegated-prepare', help='Prepare public keys on a worker.')
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument(
        '--grantor', required=True, help='Expected owner, e.g. @alice or subject ID.'
    )
    create = identity.add_parser('delegated-create', help='Authorize a prepared task identity.')
    create.add_argument('--request', type=Path, required=True)
    create.add_argument('--grants', type=Path, required=True)
    create.add_argument('--minutes', type=int, required=True)
    create.add_argument('--depth', type=int, default=0)
    create.add_argument('--max-uses', type=int)
    create.add_argument('--output', type=Path, required=True)
    accept = identity.add_parser('delegated-accept', help='Load task authorization on its worker.')
    accept.add_argument('--grant', type=Path, required=True)
    status = identity.add_parser('delegated-status', help='Inspect a task identity as its owner.')
    status.add_argument('id')
    revoke = identity.add_parser('delegated-revoke', help='Revoke a task and its descendants.')
    revoke.add_argument('id', help='Delegation ID from the issued authorization.')


async def run_command(client, args):
    state = client.state
    if args.action in {'delegated-prepare', 'delegated-accept'}:
        require(
            client.signer_override is None
            and not state.data.get('api_key')
            and not state.file('oauth-session.json').exists(),
            'delegated_profile_must_be_empty',
        )
    if args.action == 'delegated-prepare':
        require(state.subject is None and state.token is None, 'delegated_profile_must_be_empty')
        require(not state.hardware_path.exists(), 'delegated_profile_must_be_empty')
        if state.signer is None:
            state.save_signer(Ed25519Signer.generate())
        expected = state.data.get('delegated_grantor')
        require(expected is None or expected == args.grantor, 'delegated_grantor_mismatch')
        state.data['delegated_grantor'] = args.grantor
        state._save()
        recipient = state.ensure_encryption_key()
        public = b64(state.signer.public_key)
        prepared = {
            'target_service': state.server,
            'grantor': args.grantor,
            'public_key': public,
            'encryption_recipient': recipient,
            'possession_proof': wire(
                state.signer.sign(
                    possession_body(state.server, public, recipient, args.grantor),
                    purpose='delegated-identity-v1',
                )
            ),
        }
        durable_write(args.output, canonical(prepared), mode=0o600)
        return {'prepared': True, 'request_file': str(args.output)}
    if args.action == 'delegated-create':
        require(state.signer is not None and state.subject is not None, 'signature_required')
        prepared = read_json(args.request)
        require(
            isinstance(prepared, dict) and prepared.get('target_service') == state.server,
            'wrong_service',
        )
        require(0 < args.minutes <= 1440, 'invalid_delegation_minutes')
        grants = read_json(args.grants)
        require(isinstance(grants, list) and bool(grants), 'delegation_grants_required')
        # Resolve owner-readable paths to stable resource IDs before signing.
        for grant in grants:
            require(
                isinstance(grant, dict) and isinstance(grant.get('scope'), dict),
                'invalid_delegation_grant',
            )
            scope = grant['scope']
            target = scope.get('resource_id')
            if isinstance(target, str) and target.startswith('/'):
                meta = client.checked(
                    await client.call('discovery.get', {'id': target, 'view': 'meta'})
                )
                scope['resource_id'] = meta.data['id']
        arguments = {
            k: prepared[k]
            for k in ('grantor', 'public_key', 'encryption_recipient', 'possession_proof')
        }
        arguments.update(grants=grants, ttl=args.minutes * 60, depth=args.depth)
        if args.max_uses is not None:
            arguments['max_uses'] = args.max_uses
        # Persist the public request ID before issuing so response loss is replayable.
        journal = state.file('delegated-issuance-' + state.signer.key_id + '.json')
        intention = {'arguments': arguments, 'output': str(args.output.absolute())}
        if journal.exists():
            saved = read_json(journal)
            require(saved['intention'] == intention, 'delegated_issuance_pending')
            request_id = saved['request_id']
        else:
            request_id = uuid4().hex
            durable_write(
                journal, canonical({'intention': intention, 'request_id': request_id}), mode=0o600
            )
        result = await client.call('identity.delegated_create', arguments, request_id=request_id)
        if result.status == 'error':
            journal.unlink()
        result = client.checked(result)
        durable_write(args.output, canonical(result.data), mode=0o600)
        journal.unlink()
        return result
    if args.action == 'delegated-accept':
        data = read_json(args.grant)
        require(
            state.subject is None or state.data.get('delegated_identity') == data,
            'delegated_profile_must_be_empty',
        )
        require(state.signer is not None and state.token is None, 'delegated_keys_required')
        require(
            isinstance(data, dict) and data.get('target_service') == state.server, 'wrong_service'
        )
        require(
            data.get('key_id') == state.signer.key_id
            and data.get('subject_id') == subject_id(state.signer.public_key)
            and data.get('encryption_recipient') == state.encryption_recipient,
            'delegated_key_mismatch',
        )
        require(
            state.data.get('delegated_grantor')
            in {data.get('grantor'), data.get('grantor_address')},
            'delegated_grantor_mismatch',
        )
        require(parse_time(data['expires_at']) > client.clock(), 'credential_expired')
        require(
            isinstance(data.get('certificate_id'), str) and isinstance(data.get('grantor'), str),
            'invalid_delegation_grant',
        )
        state.data.update(
            subject_id=data['grantor'],
            certificates=[data['certificate_id']],
            delegated_identity=data,
            handle=data['grantor_address'].removeprefix('@'),
        )
        state._save()
        return {'accepted': True, 'address': data['address'], 'expires_at': data['expires_at']}
    if args.action == 'delegated-status':
        target = '/' + args.id if args.id.startswith('@') else args.id
        return await client.call('identity.delegated_get', {'id': target})
    return await client.call('identity.delegation_revoke', {'id': args.id})
