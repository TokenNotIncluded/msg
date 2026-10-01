"""Local account selection; hardware and software identities share one layout."""

import os

from msg.atomic_file import durable_write
from msg.client import ClientState, private_client_json
from msg.core.codec import canonical
from msg.core.errors import require
from msg.paths import ClientPaths, private_directory
from msg.service_origin import service_origin


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
        previous = ClientPaths.discover(profile=args.profile)
        server = (
            args.server
            or os.environ.get('MSG_SERVER')
            or (private_client_json(previous.config / 'service.json') or {}).get('server')
        )
        require(server is not None, 'server_required')
        server = service_origin(server)
        paths = ClientPaths.discover(server=server, account=args.name)
        saved = private_client_json(paths.state / 'client.json')
        require(saved is not None, 'local_account_not_found')
        selected = ClientState(server=server, profile=args.profile, account=args.name)
        require(selected.subject is not None, 'local_account_not_authenticated')
        marker = ClientPaths.discover(server=server).config / 'current-account.json'
        private_directory(marker.parent)
        durable_write(marker, canonical({'version': 1, 'account': args.name}), mode=0o600)
        return {'account': args.name, 'server': server, 'handle': selected.data.get('handle')}
    # Resolve the selected service and safely migrate its old singleton layout.
    state = ClientState(server=args.server, profile=args.profile, account=args.account)
    service = ClientPaths.discover(server=state.server)
    marker = service.config / 'current-account.json'
    selection = private_client_json(marker) or {'account': 'default'}
    root = service.state / 'accounts'
    result = []
    for directory in sorted(root.iterdir()):
        require(not directory.is_symlink(), 'unsafe_client_directory')
        if not directory.is_dir():
            continue
        # Validate each account name and directory without loading/exporting keys.
        paths = ClientPaths.discover(server=state.server, account=directory.name)
        saved = private_client_json(paths.state / 'client.json')
        if saved is None:
            continue
        result.append({
            'account': directory.name,
            'handle': saved.get('handle'),
            'subject_id': saved.get('subject_id'),
            'selected': directory.name == selection['account'],
            'signer': 'yubikey'
            if paths.file('hardware-signer.json').exists()
            else 'software'
            if paths.file('identity.key').exists()
            else None,
        })
    return {'server': state.server, 'accounts': result}
