"""Inspect and approve native browser access using a locally held signing key."""

from msg.client_accounts import identity_status
from msg.core.errors import require


async def run_command(client, args):
    if args.command == 'login' or args.action == 'status':
        status = identity_status(client.state, signer_override=client.signer_override)
        return {
            **status,
            'browser_login': client.state.server + '/login',
            'instructions': 'Keep your private key on this device. Open /login, inspect its code with msg auth request CODE, then use msg auth approve CODE. New identities use msg register HANDLE.',
        }
    if args.action == 'request':
        return await client.call(
            'identity.session_request', {'user_code': args.user_code}, anonymous=True
        )
    signer = client.signer_override or client.state.signer
    require(signer is not None and client.state.subject is not None, 'signing_key_required')
    return await client.call(
        'identity.session_approve',
        {'user_code': args.user_code, 'decision': 'deny' if args.action == 'deny' else 'approve'},
        signer=signer,
    )
