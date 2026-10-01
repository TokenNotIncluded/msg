"""Explicit, local-only service defaults; credentials stay per service/account."""

import os

from msg.atomic_file import durable_write
from msg.client import private_client_json
from msg.core.codec import canonical
from msg.core.errors import require
from msg.paths import ClientPaths, private_directory
from msg.service_origin import service_origin


def run_command(args):
    require(args.config_dir is None, 'server_conflicts_with_config_dir')
    require(args.migrate_from is None, 'server_conflicts_with_migration')
    paths = ClientPaths.discover(profile=args.profile)
    marker = paths.config / 'service.json'
    saved = private_client_json(marker)
    if saved is not None:
        require(saved.get('version') == 1, 'unknown_client_state_version')
    default = service_origin(saved.get('server')) if saved is not None else None
    if args.action == 'use':
        default = service_origin(args.url)
        require(
            args.server is None or service_origin(args.server) == default,
            'connection_server_conflict',
        )
        private_directory(ClientPaths.discover().config)
        if args.profile is not None:
            private_directory(paths.config.parent)
        private_directory(marker.parent)
        durable_write(marker, canonical({'version': 1, 'server': default}), mode=0o600)
    override = args.server or os.environ.get('MSG_SERVER')
    return {
        'default_server': default,
        'server': service_origin(override) if override else default,
        'source': '--server' if args.server else 'MSG_SERVER' if override else 'saved',
        'profile': args.profile,
    }
