"""Local account selection; hardware and software identities share one layout."""

import os
import tempfile
from pathlib import Path

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
    if args.action in {'backup', 'restore'}:
        from msg.client_account_backup import backup_account, restore_account

        require(args.server is not None, 'account_backup_server_required')
        require(args.profile is None, 'account_backup_profile_not_supported')
        require(getattr(args, 'key', None) is None, 'account_backup_external_signer_not_supported')
        require(
            getattr(args, 'migrate_from', None) is None, 'account_backup_migration_not_supported'
        )
        if args.action == 'backup':
            require(args.account is not None, 'account_backup_account_required')
            require(args.output is not None, 'account_backup_output_required')
            require(not getattr(args, 'recovery_hint', None), 'account_backup_publish_required')
            return backup_account(args.server, args.account, args.recipient, args.output)
        require(args.account is None, 'account_restore_account_conflict')
        require(args.expected_subject is not None, 'account_backup_expected_subject_required')
        return restore_account(
            args.server,
            args.name,
            args.input,
            args.identity,
            expected_subject=args.expected_subject,
            expected_key_id=args.expected_key_id,
            expected_sha256=args.expected_sha256,
        )
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


async def run_backup_command(args, *, transport_factory, client_factory):
    """Explicit public ciphertext publication, or anonymous profile restore discovery."""
    from msg.client_account_backup import backup_account, ciphertext_digest_pin, restore_account
    from msg.client_backup_remote import fetch_backup, publish_backup

    require(args.config_dir is None, 'account_conflicts_with_config_dir')
    require(args.server is not None, 'account_backup_server_required')
    require(args.profile is None, 'account_backup_profile_not_supported')
    require(args.key is None, 'account_backup_external_signer_not_supported')
    require(args.migrate_from is None, 'account_backup_migration_not_supported')
    server = service_origin(args.server)
    if args.action == 'fetch':
        expected = ciphertext_digest_pin(args.expected_sha256)
        manifest = await fetch_backup(
            server,
            args.from_profile,
            args.output,
            expected_sha256=expected.removeprefix('sha256:') if expected is not None else None,
        )
        return {
            'server': server,
            'source_profile': args.from_profile,
            'ciphertext_sha256': 'sha256:' + manifest['file']['sha256'],
            'output': str(args.output.expanduser().absolute()),
            'manifest': manifest,
            'decrypted': False,
        }
    if args.action == 'publish':
        require(args.account is not None, 'account_backup_account_required')
        state = ClientState(server=server, account=args.account)
        require(state.subject is not None and state.signer is not None, 'signing_identity_required')
        options = {'endpoint': args.endpoint} if args.endpoint is not None else {}
        transport = transport_factory(server, **options)
        client = client_factory(state, transport)
        try:
            manifest = await publish_backup(
                client,
                args.input,
                {
                    'server': server,
                    'subject_id': state.subject,
                    'key_id': state.signer.key_id,
                    'archive_format': args.archive_format,
                    'encryption_format': args.encryption,
                },
                recovery_hint=args.recovery_hint,
            )
        finally:
            await transport.close()
        return {
            'server': server,
            'account': args.account,
            'public_ciphertext': True,
            'manifest': manifest,
        }
    with tempfile.TemporaryDirectory(prefix='msg-account-backup-') as directory:
        temporary = Path(directory) / 'identity.age'
        if args.action == 'backup':
            require(args.account is not None, 'account_backup_account_required')
            ciphertext = args.output or temporary
            receipt = backup_account(server, args.account, args.recipient, ciphertext)
            state = ClientState(server=server, account=args.account)
            require(
                state.subject == receipt['subject_id']
                and state.signer is not None
                and state.signer.key_id == receipt['key_id'],
                'account_backup_identity_mismatch',
            )
            options = {'endpoint': args.endpoint} if args.endpoint is not None else {}
            transport = transport_factory(server, **options)
            client = client_factory(state, transport)
            try:
                metadata = {
                    key: receipt[key]
                    for key in (
                        'server',
                        'subject_id',
                        'key_id',
                        'ciphertext_sha256',
                        'client_version',
                    )
                } | {'archive_format': 'msg.account-backup/1'}
                metadata['ciphertext_sha256'] = metadata['ciphertext_sha256'].removeprefix(
                    'sha256:'
                )
                published = await publish_backup(
                    client,
                    ciphertext,
                    metadata,
                    recovery_hint=args.recovery_hint,
                )
            finally:
                await transport.close()
            if args.output is None:
                receipt.pop('output', None)
            return receipt | {'published': published, 'public_ciphertext': True}
        require(
            args.action == 'restore' and args.account is None, 'account_restore_account_conflict'
        )
        expected = ciphertext_digest_pin(args.expected_sha256)
        manifest = await fetch_backup(
            server,
            args.from_profile,
            temporary,
            expected_sha256=expected.removeprefix('sha256:') if expected is not None else None,
        )
        require(
            manifest['archive_format'] == 'msg.account-backup/1'
            and manifest['encryption']['format'] == 'age',
            'account_backup_automatic_restore_not_supported',
        )
        return restore_account(
            server,
            args.name,
            temporary,
            args.identity,
            expected_subject=args.expected_subject or manifest['subject_id'],
            expected_key_id=args.expected_key_id or manifest['key_id'],
            expected_sha256=args.expected_sha256 or manifest['file']['sha256'],
        ) | {'source_profile': args.from_profile, 'manifest': manifest}
