"""Local account selection; hardware and software identities share one layout."""

import os

from msg.atomic_file import durable_write
from msg.client import ClientState, private_client_json
from msg.core.codec import canonical
from msg.core.errors import require
from msg.paths import ClientPaths, private_directory
from msg.service_origin import service_origin


def identity_status(state, *, signer_override=None):
    """Report configured request credentials without opening sessions or contacting hardware."""
    session = state.file('oauth-session.json')
    if signer_override is not None:
        auth = 'signature'
    elif session.exists() or session.is_symlink():
        auth = 'oauth'
    elif state.data.get('token'):
        auth = 'token'
    elif state.data.get('api_key'):
        auth = 'api-key'
    else:
        auth = 'signature' if state.signer is not None else 'none'
    signer = signer_override or state.signer
    return {
        'account': state.account,
        'handle': state.data.get('handle'),
        'subject_id': state.subject,
        'key_id': signer.key_id if signer is not None else None,
        'server': state.server,
        'certificates': state.certificates,
        'auth': auth,
    }


def selected_service(args):
    previous = ClientPaths.discover(profile=args.profile)
    selection = private_client_json(previous.config / 'service.json')
    if selection is not None:
        require(selection.get('version') == 1, 'unknown_client_state_version')
    server = args.server or os.environ.get('MSG_SERVER') or (selection or {}).get('server')
    if server is None:
        legacy = private_client_json(previous.state / 'client.json')
        if legacy is None:
            legacy = private_client_json(previous.config / 'client.json')
        server = (legacy or {}).get('server')
    require(server is not None, 'server_required')
    return service_origin(server)


def run_command(args):
    require(args.config_dir is None, 'account_conflicts_with_config_dir')
    if args.action == 'import':
        source = args.directory.expanduser().absolute()
        if args.server is not None:
            old = ClientPaths.discover(server=args.server)
            if source in {old.data, old.state} and (old.state / 'client.json').is_file():
                source = old
        state = ClientState(
            server=args.server,
            profile=args.profile,
            account=args.name,
            migrate_from=source,
        )
        return {
            'account': state.account,
            'server': state.server,
            'handle': state.data.get('handle'),
        }
    if args.action == 'use':
        server = selected_service(args)
        paths = ClientPaths.discover(server=server, account=args.name)
        saved = private_client_json(paths.state / 'client.json')
        require(saved is not None, 'local_account_not_found')
        require(saved.get('subject_id') is not None, 'local_account_not_authenticated')
        selected = ClientState(server=server, profile=args.profile, account=args.name)
        require(selected.subject is not None, 'local_account_not_authenticated')
        marker = ClientPaths.discover(server=server).config / 'current-account.json'
        private_directory(marker.parent)
        durable_write(marker, canonical({'version': 1, 'account': args.name}), mode=0o600)
        return {'account': args.name, 'server': server, 'handle': selected.data.get('handle')}
    server = selected_service(args)
    service = ClientPaths.discover(server=server)
    marker = service.config / 'current-account.json'
    selection = private_client_json(marker)
    if selection is not None:
        require(selection.get('version') == 1, 'unknown_client_state_version')
        ClientPaths.discover(server=server, account=selection.get('account', ''))
    names = service.account_names()
    selected = (selection or {}).get('account')
    if args.account is not None:
        ClientPaths.discover(server=server, account=args.account)
        require(args.account in names, 'local_account_not_found')
        selected = args.account
    result = []
    for name in names:
        paths = ClientPaths.discover(server=server, account=name)
        saved = private_client_json(paths.state / 'client.json')
        if saved is not None:
            require(saved.get('version') == 1, 'unknown_client_state_version')
            require(service_origin(saved.get('server')) == server, 'client_server_mismatch')
        saved = saved or {}
        result.append({
            'account': name,
            'handle': saved.get('handle'),
            'subject_id': saved.get('subject_id'),
            'selected': name == selected,
            'signer': 'yubikey'
            if paths.file('hardware-signer.json').exists()
            else 'software'
            if paths.file('identity.key').exists()
            else None,
        })
    return {'server': server, 'accounts': result}
