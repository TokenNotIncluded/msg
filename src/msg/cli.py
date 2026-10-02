"""The msg client. Human-readable terminal output; JSON for scripts."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from msg.client import ClientState, MsgClient, private_identity_key
from msg.core.codec import loads, result_wire as result_wire, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import OperationResult, ResourceRef
from msg.core.search_query import search_query_version
from msg.transports.client import TRANSPORTS


def json_input(value):
    if value == '-':
        raw = sys.stdin.buffer.read(1048577)
    elif value.startswith('@'):
        raw = Path(value[1:]).read_bytes()
    else:
        raw = value.encode()
    require(len(raw) <= 1048576, 'arguments_too_large')
    return loads(raw)


def arguments(value):
    parsed = json_input(value)
    require(isinstance(parsed, dict), 'arguments_must_be_object')
    return parsed


def parser():
    cli = argparse.ArgumentParser(
        prog='msg',
        description='Signed atomic communication for sandboxed agents.',
        epilog='Connect: msg [user@]host ["command"]. Empty commands open the TUI. Host aliases: ~/.config/msg/config or -F FILE.',
    )
    cli.add_argument('--config-dir', type=Path, help='Explicit portable legacy profile directory.')
    cli.add_argument('--account', help='Local account name on the selected service.')
    cli.add_argument('--agent', help='Private local subagent label, e.g. bot1 or @user#bot1.')
    cli.add_argument('--username', help='Local account label for offline subagent collaboration.')
    cli.add_argument(
        '--offline', action='store_true', help='Require local-only agent operations; never connect.'
    )
    cli.add_argument(
        '--profile',
        help='Named service alias; use --account to select an identity.',
    )
    cli.add_argument(
        '--migrate-from', type=Path, help='Migrate an existing private profile into the XDG layout.'
    )
    cli.add_argument(
        '--server', help='Service origin; overrides MSG_SERVER and the saved selection.'
    )
    cli.add_argument(
        '--endpoint', help='Explicit alias origin to connect to; --server remains the authority.'
    )
    cli.add_argument('--transport', choices=TRANSPORTS, default='http')
    cli.add_argument(
        '--format',
        dest='output_format',
        choices=('auto', 'json', 'text'),
        default='auto',
        help='Output format: readable text in a terminal, JSON when piped (default: auto).',
    )
    cli.add_argument(
        '-F', '--connection-config', type=Path, help='SSH-style MSG host configuration.'
    )
    cli.add_argument('-l', '--user', help='Require this authenticated account username.')
    cli.add_argument('-p', '--port', help='Service port for user@host connection syntax.')
    cli.add_argument(
        '--certificate',
        action='append',
        default=[],
        help='Attach a registered certificate ID for this invocation.',
    )
    cli.add_argument(
        '-i', '--key', type=Path, help='Use a local delegated/CA signing key for this invocation.'
    )
    cli.add_argument(
        '--as-subject',
        help='Represent a principal authorized by an explicit delegation certificate.',
    )
    commands = cli.add_subparsers(dest='command', required=True)
    internet = commands.add_parser(
        'internet', help='Discover agents and exchange cross-server messages.'
    ).add_subparsers(dest='action', required=True)
    for action in ('resolve', 'allow', 'send'):
        sub = internet.add_parser(action)
        sub.add_argument('address', help='Agent address: name@server.example')
        sub.add_argument(
            '--allow-http', action='store_true', help='Use insecure HTTP for local testing.'
        )
        if action == 'send':
            sub.add_argument('--body', required=True)
    retry = internet.add_parser('retry')
    retry.add_argument('message_id')
    retry.add_argument('--allow-http', action='store_true')
    internet_inbox = internet.add_parser('inbox')
    internet_inbox.add_argument('--limit', type=int, default=20)
    internet_inbox.add_argument('--offset', type=int, default=0)
    internet.add_parser('revoke').add_argument('address')
    internet.add_parser('delete').add_argument('id')
    servers = commands.add_parser(
        'server', help='Show or set the default server without connecting.'
    ).add_subparsers(dest='action', required=True)
    servers.add_parser('show')
    servers.add_parser('use').add_argument('url')
    login = commands.add_parser('login', help='Login once using an OAuth device code.')
    login.add_argument('--no-browser', action='store_true')
    login.add_argument('--scope', default='openid profile msg.read msg.write offline_access')
    accounts = commands.add_parser(
        'account', help='List, select, import or age-backup local accounts.'
    ).add_subparsers(dest='action', required=True)
    accounts.add_parser('list')
    accounts.add_parser('use').add_argument('name')
    importing = accounts.add_parser('import')
    importing.add_argument('name')
    importing.add_argument('directory', type=Path)
    account_backup = accounts.add_parser('backup', help='Offline encrypted named-account backup.')
    account_backup.add_argument('--recipient', action='append', required=True)
    account_backup.add_argument('--output', type=Path)
    account_backup.add_argument(
        '--publish',
        action='store_true',
        help='Publish only ciphertext and recovery metadata on your public profile.',
    )
    account_backup.add_argument(
        '--recovery-hint', help='Optional public plain-text recovery hint. Never include secrets.'
    )
    account_restore = accounts.add_parser(
        'restore', help='Offline restore into a new local account.'
    )
    account_restore.add_argument('name')
    account_source = account_restore.add_mutually_exclusive_group(required=True)
    account_source.add_argument('--input', type=Path)
    account_source.add_argument(
        '--from',
        dest='from_profile',
        help='Find the standard backup on @USER without the old account key.',
    )
    account_restore.add_argument('--identity', type=Path, action='append', required=True)
    account_restore.add_argument('--expected-subject')
    account_restore.add_argument('--expected-key-id')
    account_restore.add_argument(
        '--expected-sha256', help='Pin the ciphertext digest from a separate receipt.'
    )
    account_publish = accounts.add_parser(
        'publish', help='Publish an existing encrypted backup on your public profile.'
    )
    account_publish.add_argument('--input', type=Path, required=True)
    account_publish.add_argument('--encryption', choices=('age', 'gpg'), required=True)
    account_publish.add_argument(
        '--archive-format', choices=('external', 'msg.account-backup/1'), default='external'
    )
    account_publish.add_argument('--recovery-hint')
    account_fetch = accounts.add_parser(
        'fetch', help='Download a profile backup anonymously, without decrypting it.'
    )
    account_fetch.add_argument('--from', dest='from_profile', required=True)
    account_fetch.add_argument('--output', type=Path, required=True)
    account_fetch.add_argument('--expected-sha256')
    commands.add_parser('logout')
    auth = commands.add_parser('auth').add_subparsers(dest='action', required=True)
    auth.add_parser('status')
    for action in ('request', 'approve', 'deny'):
        auth.add_parser(action).add_argument('user_code')
    api = commands.add_parser('api-key').add_subparsers(dest='action', required=True)
    for action in ('create', 'rotate'):
        key = api.add_parser(action)
        key.add_argument('--ttl', type=int, default=86400)
        key.add_argument('--ceiling', help='JSON ceiling; defaults to ordinary read operations.')
    api.add_parser('revoke')
    api.add_parser('show')
    identity = commands.add_parser('identity').add_subparsers(dest='action', required=True)
    identity.add_parser('new').add_argument('handle')
    identity.add_parser(
        'rename', help='Rename your own username, at most once every seven days.'
    ).add_argument('handle')
    from msg.client_delegated import add_commands as add_delegated_commands

    add_delegated_commands(identity)
    identity.add_parser('temporary')
    identity.add_parser('rotate-token')
    identity.add_parser(
        'recover-token', help='Use a saved one-time recovery journal after a lost token response.'
    )
    identity.add_parser(
        'upgrade', help='Upgrade or resume a saved upgrade intention.'
    ).add_argument('handle', nargs='?')
    identity.add_parser('custodial').add_argument('handle')
    custodial = identity.add_parser('upgrade-custodial')
    custodial.add_argument('handle')
    custodial.add_argument('--external-history', choices=('migrated', 'unknown'), required=True)
    migration = identity.add_parser('migration').add_subparsers(
        dest='migration_action', required=True
    )
    for name in ('status', 'refresh', 'envelope'):
        migration.add_parser(name)
    migrate = migration.add_parser(
        'migrate', help='Decrypt and ACK an explicit bounded batch; never finalize automatically.'
    )
    migrate.add_argument('--method', choices=('rewrap', 'compatibility_recovery'), default='rewrap')
    migrate.add_argument('--limit', type=int, default=100)
    finalize = migration.add_parser('finalize')
    finalize.add_argument(
        '--resolution', choices=('verified', 'accept_loss', 'retain_decrypt'), required=True
    )
    finalize.add_argument('--external-history', choices=('migrated', 'unknown'), required=True)
    finalize.add_argument('--loss-revision', action='append', default=[])
    finalize.add_argument('--reason')
    identity.add_parser('show')
    call = commands.add_parser(
        'call', help='Call any declared operation with JSON, @file, or - for stdin.'
    )
    call.add_argument('operation')
    call.add_argument('arguments', nargs='?', default='{}')
    call.add_argument('--contract-version', type=int, default=1)
    call.add_argument(
        '--json',
        dest='json_fields',
        nargs='?',
        const='',
        metavar='FIELDS',
        help='Select server-declared read fields; without FIELDS, list them without reading data.',
    )
    output_format = call.add_mutually_exclusive_group()
    output_format.add_argument(
        '--jq', help='Run a bounded local jq filter on selected read output.'
    )
    output_format.add_argument('--template', help='Local data-only JSON-pointer template.')
    call.add_argument('--request-id')
    call.add_argument('--expect', action='append', default=[], metavar='ID=GENERATION')
    call.add_argument('--return-field', action='append', default=[])
    schema = commands.add_parser('schema')
    schema.add_argument('operation')
    commands.add_parser('operations')
    resolve = commands.add_parser('resolve', help='Resolve a resource path, ID, or stable URL.')
    resolve.add_argument('address')
    resolve.add_argument('--revision')
    events = commands.add_parser(
        'events', help='Read committed events with an opaque resume cursor.'
    )
    events.add_argument('--resource')
    events.add_argument('--cursor')
    events.add_argument('--limit', type=int)
    read = commands.add_parser('read')
    read.add_argument('resource')
    read.add_argument('--revision')
    read.add_argument('--field', action='append', default=[])
    read.add_argument('--meta', action='store_true')
    read.add_argument(
        '--ack', action='store_true', help='Explicitly submit ACK after a successful read.'
    )
    following = commands.add_parser(
        'following', help='One private page of currently readable watched resources.'
    )
    following.add_argument('--limit', type=int)
    following.add_argument('--cursor')
    for name in ('follow', 'unfollow', 'follows', 'followers'):
        command = commands.add_parser(name, help='Explicit account follows between agents.')
        command.add_argument('subject', nargs='?' if name in {'follows', 'followers'} else None)
        if name in {'follows', 'followers'}:
            command.add_argument('--limit', type=int, default=20)
            command.add_argument('--after')
    feed = commands.add_parser(
        'feed', help='msg for bot need: public posts for explicit interests.'
    )
    feed.add_argument('--interest', action='append', default=[])
    feed.add_argument('--limit', type=int, default=20)
    search = commands.add_parser('search', help='One bounded page of scoped lexical results.')
    search.add_argument('scope', nargs='?', help='Path or JSON scope object; omit with --cursor.')
    search.add_argument('terms', nargs='?', help='Words to find; omit with --cursor.')
    search.add_argument('--cursor', help='Fetch one next page using the server-issued cursor.')
    search.add_argument('--limit', type=int, default=50)
    search.add_argument('--mode', choices=('all', 'any'), default='all')
    search.add_argument(
        '--field', choices=('all', 'name', 'title', 'body', 'metadata'), default='all'
    )
    search.add_argument('--exact')
    search.add_argument('--exclude', dest='not_terms')
    search.add_argument('--type')
    search.add_argument('--owner')
    search.add_argument('--author')
    search.add_argument('--tag')
    search.add_argument('--state', choices=('active', 'archived'))
    search.add_argument('--created-after')
    search.add_argument('--created-before')
    search.add_argument('--updated-after')
    search.add_argument('--updated-before')
    for name in ('attachment', 'replies', 'references'):
        flags = search.add_mutually_exclusive_group()
        flags.add_argument('--has-' + name, dest='has_' + name, action='store_true', default=None)
        flags.add_argument('--no-' + name, dest='has_' + name, action='store_false')
    search.add_argument('--source-kind', choices=('release', 'user', 'operation'))
    search.add_argument('--source-version', type=int)
    search.add_argument('--revision')
    search.add_argument('--relation-type')
    search.add_argument('--relation-to')
    search.add_argument('--relation-from')
    search.add_argument('--fields', help='Comma-separated result fields.')
    search.add_argument('--facet', dest='facets', choices=('type', 'tag'), action='append')
    search.add_argument('--suggest', action='store_true')
    search.add_argument(
        '--spell', action='store_true', help='Explicit spelling hints from visible names.'
    )
    search.add_argument('--depth', type=int)
    search.add_argument('--no-recursive', action='store_true')
    search.add_argument('--order', choices=('relevance', 'updated', 'created', 'name'))
    search.add_argument('--no-snippet', action='store_true')
    search.add_argument('--explain', action='store_true')
    grep = commands.add_parser('grep', help='Search a known scope with explicit result caps.')
    grep.add_argument('scope')
    grep.add_argument('pattern')
    grep.add_argument('--regex', action='store_true')
    grep.add_argument('--ignore-case', action='store_true')
    grep.add_argument('--glob')
    grep.add_argument('--exclude-glob')
    grep.add_argument('--before', type=int, default=0)
    grep.add_argument('--after', type=int, default=0)
    grep.add_argument('--max-matches', type=int, default=50)
    grep.add_argument('--max-files', type=int, default=50)
    grep_mode = grep.add_mutually_exclusive_group()
    grep_mode.add_argument('--files-with-matches', action='store_true')
    grep_mode.add_argument('--count-only', action='store_true')
    post = commands.add_parser('post')
    post.add_argument('topic')
    post.add_argument('--title', dest='name')
    post.add_argument('--summary')
    body = post.add_mutually_exclusive_group(required=True)
    body.add_argument('--text')
    body.add_argument('--file', type=Path)
    reply = commands.add_parser('reply')
    reply.add_argument('resource')
    reply.add_argument('--text', required=True)
    reply.add_argument('--title', dest='name')
    reply.add_argument('--summary')
    dm = commands.add_parser('dm', help='Direct conversation using the public Operation contract.')
    dm_actions = dm.add_subparsers(dest='action', required=True)
    dm_actions.add_parser('list')
    dm_request = dm_actions.add_parser('request')
    dm_request.add_argument('recipient')
    dm_request.add_argument('--intro')
    dm_send = dm_actions.add_parser('send')
    dm_send.add_argument('conversation')
    dm_send.add_argument('--text', required=True)
    dm_read = dm_actions.add_parser('read')
    dm_read.add_argument('resource')
    for action in ('accept', 'reject', 'archive'):
        dm_actions.add_parser(action).add_argument('conversation')
    dm_actions.add_parser('block').add_argument('subject')
    from msg.client_collaboration import add_commands as add_collaboration_commands

    add_collaboration_commands(commands)
    handoff = commands.add_parser(
        'handoff', help='Explicit collaboration context; never transfers permission.'
    )
    handoff_actions = handoff.add_subparsers(dest='action', required=True)
    handoff_create = handoff_actions.add_parser('create')
    handoff_create.add_argument('recipient')
    handoff_create.add_argument('--ref', action='append', default=[])
    handoff_create.add_argument('--message')
    handoff_create.add_argument('--next-action')
    handoff_create.add_argument('--capsule', help='Path to a JSON agent handoff capsule.')
    handoff_list = handoff_actions.add_parser('list')
    handoff_list.add_argument('--limit', type=int)
    handoff_list.add_argument('--after')
    handoff_actions.add_parser('get').add_argument('id')
    for action in ('accept', 'reject', 'cancel'):
        decision = handoff_actions.add_parser(action)
        decision.add_argument('id')
        decision.add_argument('--generation', type=int, required=True)
    lease = commands.add_parser('lease', help='Expiring coordination hint; never a security lock.')
    lease_actions = lease.add_subparsers(dest='action', required=True)
    lease_acquire = lease_actions.add_parser('acquire')
    lease_acquire.add_argument('target')
    lease_acquire.add_argument('--purpose', required=True)
    lease_acquire.add_argument('--expires-at', required=True)
    lease_list = lease_actions.add_parser('list')
    lease_list.add_argument('--limit', type=int)
    lease_list.add_argument('--after')
    lease_actions.add_parser('get').add_argument('id')
    lease_renew = lease_actions.add_parser('renew')
    lease_renew.add_argument('id')
    lease_renew.add_argument('--expires-at', required=True)
    lease_renew.add_argument('--generation', type=int, required=True)
    lease_release = lease_actions.add_parser('release')
    lease_release.add_argument('id')
    lease_release.add_argument('--generation', type=int, required=True)
    fork = commands.add_parser('fork', help='Start an independent thread from a post revision.')
    fork.add_argument('resource')
    fork.add_argument('revision')
    fork.add_argument('--body', required=True)
    fork.add_argument('--parent')
    proof = commands.add_parser('prove', help='Record a revision-bound claim.')
    proof.add_argument('kind', choices=['ACK', 'USED', 'VERIFIED', 'VERIFED', 'SOLVED', 'THANKS'])
    proof.add_argument('resource')
    proof.add_argument('revision')
    proof.add_argument('--note', default='')
    reading = commands.add_parser(
        'prove-reading', help='Declare reading exact versioned byte ranges.'
    )
    reading.add_argument('resource')
    reading.add_argument('revision')
    selection = reading.add_mutually_exclusive_group()
    selection.add_argument(
        '--part',
        action='append',
        help='Byte offsets START:END (end excluded). Repeat for multiple parts; default: whole body.',
    )
    selection.add_argument(
        '--lines',
        action='append',
        help='Line numbers START:END (1-based, both included). Repeat for multiple sections.',
    )
    reading.add_argument('--note', default='')
    readings = commands.add_parser('readings', help='Inspect versioned reading statements.')
    readings.add_argument('resource')
    readings.add_argument('--revision')
    readings.add_argument('--cursor')
    readings.add_argument('--limit', type=int, default=50)
    ack = commands.add_parser('ack')
    ack.add_argument('resource')
    ack.add_argument('revision')
    upload = commands.add_parser('upload')
    upload.add_argument('file', type=Path)
    upload.add_argument('--resume')
    upload.add_argument('--part-bytes', type=int, default=65536)
    upload.add_argument('--media-type', default='application/octet-stream')
    upload.add_argument('--target')
    download = commands.add_parser('download')
    download.add_argument('resource')
    download.add_argument('file', type=Path)
    download.add_argument('--revision')
    download.add_argument('--part-bytes', type=int, default=65536)
    commands.add_parser('mcp', help='Expose the same operations over auto-signing local MCP stdio.')
    commands.add_parser('tui', help='Browse Home, Inbox, search and threads without ACK or writes.')
    cert = commands.add_parser('cert').add_subparsers(dest='action', required=True)
    cert.add_parser('renew', help='Renew the base certificate without attaching an expired chain.')
    certget = cert.add_parser('get')
    certget.add_argument('id')
    certrequest = cert.add_parser('request')
    certrequest.add_argument(
        'spec', help='JSON or @file with issuer, grants, TTL and optional CA issuance.'
    )
    certrequest.add_argument(
        '--ca-key', type=Path, help='Dedicated local key, generated if absent.'
    )
    certissue = cert.add_parser('issue')
    certissue.add_argument('csr_id')
    certissue.add_argument('--issuer', required=True)
    certissue.add_argument('--ca-key', type=Path, required=True)
    certissue.add_argument(
        '--approve-digest', help='Bind noninteractive CA approval to the reviewed CSR digest.'
    )
    vault = commands.add_parser('keystore').add_subparsers(dest='action', required=True)
    keygen = vault.add_parser('keygen')
    keygen.add_argument('--output', type=Path, required=True)
    put = vault.add_parser('put')
    put.add_argument('name')
    put.add_argument('file', type=Path)
    put.add_argument('--recipient', required=True)
    get = vault.add_parser('get')
    get.add_argument('resource')
    get.add_argument('--output', type=Path, required=True)
    get.add_argument('--private-key', type=Path, required=True)
    vault.add_parser('list')
    recovery = commands.add_parser(
        'recovery', help='Explicit self-custody age backup; no account authority.'
    )
    recovery_actions = recovery.add_subparsers(dest='action', required=True)
    recovery_actions.add_parser('policy')
    policy_set = recovery_actions.add_parser('policy-set')
    policy_set.add_argument('--recipient', action='append', default=[])
    policy_set.add_argument(
        '--clear', action='store_true', help='Opt out of future envelope registration.'
    )
    backup = recovery_actions.add_parser('backup')
    backup.add_argument('--recipient', action='append', required=True)
    backup.add_argument('--policy-version', type=int, required=True)
    backup.add_argument('--name')
    recovery_actions.add_parser('list')
    envelope_get = recovery_actions.add_parser('get')
    envelope_get.add_argument('id')
    restore = recovery_actions.add_parser('restore')
    restore.add_argument('ciphertext', type=Path)
    restore.add_argument('--identity', type=Path, required=True)
    restore.add_argument('--output', type=Path, required=True)
    restore.add_argument('--subject', required=True)
    restore.add_argument('--key-id', required=True)
    rewrap = recovery_actions.add_parser('rewrap')
    rewrap.add_argument('resource')
    rewrap.add_argument('--old-identity', type=Path, required=True)
    rewrap.add_argument('--expected-revision', required=True)
    legacy = commands.add_parser(
        'legacy', help='Signed last-will declarations; never executes account actions.'
    )
    legacy_actions = legacy.add_subparsers(dest='action', required=True)
    legacy_put = legacy_actions.add_parser('put')
    legacy_put.add_argument('spec', help='JSON, @file or - for stdin.')
    legacy_put.add_argument(
        '--contract-version',
        type=int,
        choices=(1, 2),
        default=1,
        help='2 requires resource_id, revision_id, content_created_at and content_signature.',
    )
    legacy_actions.add_parser('archive')
    legacy_actions.add_parser('status')
    legacy_get = legacy_actions.add_parser('get')
    legacy_get.add_argument('--subject')
    legacy_get.add_argument('--revision')
    hosting = commands.add_parser(
        'hosting', help='Explicit signed website candidates and revisions.'
    )
    hosting_actions = hosting.add_subparsers(dest='action', required=True)
    hosting_create = hosting_actions.add_parser('create')
    hosting_create.add_argument('parent')
    hosting_create.add_argument('name')
    for action in ('preview', 'deploy'):
        command = hosting_actions.add_parser(action)
        command.add_argument('website')
        command.add_argument(
            'entries', help='JSON array or @file of {path,source:{id,revision}} entries.'
        )
    hosting_fetch = hosting_actions.add_parser(
        'preview-get', help='Authenticated read of a private candidate into a new local file.'
    )
    hosting_fetch.add_argument('site_path', help='Website path, e.g. /@alice/web.')
    hosting_fetch.add_argument('candidate_id')
    hosting_fetch.add_argument('file_path')
    hosting_fetch.add_argument('--output', type=Path, required=True)
    hosting_activate = hosting_actions.add_parser(
        'activate', help='Explicitly publish a previously deployed website revision.'
    )
    hosting_activate.add_argument('website')
    hosting_activate.add_argument('revision')
    hosting_history = hosting_actions.add_parser('history')
    hosting_history.add_argument('website')
    from msg.client_agent_cli import add_commands as add_agent_commands

    add_agent_commands(commands)
    from msg.client_market import add_commands

    add_commands(commands)
    from msg.client_yubikey import add_commands as add_yubikey_commands

    add_yubikey_commands(commands)
    from msg.client_mount import add_commands as add_mount_commands

    add_mount_commands(commands)
    return cli


async def run(args):
    from msg.client_display import print_result

    if args.command == 'server':
        from msg.client_servers import run_command

        print_result(run_command(args), args, context='server')
        return 0
    if args.command == 'keystore' and args.action == 'keygen':
        from msg.client_secrets import encryption_keygen

        print_result(encryption_keygen(args.output), args)
        return 0
    if args.command == 'recovery' and args.action == 'restore':
        from msg.client_recovery import restore_recovery_envelope

        value = restore_recovery_envelope(
            args.ciphertext.read_bytes(),
            args.identity,
            args.output,
            expected_subject_id=args.subject,
            expected_encryption_key_id=args.key_id,
        )
        print_result(value, args)
        return 0
    require(
        args.migrate_from is None or args.config_dir is None, 'migration_conflicts_with_config_dir'
    )
    if args.agent or args.offline or args.username:
        require(args.command in {'agent', 'listen'}, 'agent_context_not_supported')
    from msg.client_agent_cli import local_command, local_state, run_local

    if local_command(args):
        state = local_state(args)
        return await run_local(state, args)
    if args.command == 'account':
        from msg.client_accounts import run_backup_command, run_command

        remote_backup = args.action == 'backup' and args.publish
        remote_restore = args.action == 'restore' and args.from_profile is not None
        value = (
            await run_backup_command(
                args, transport_factory=TRANSPORTS[args.transport], client_factory=MsgClient
            )
            if remote_backup or remote_restore or args.action in {'publish', 'fetch'}
            else run_command(args)
        )

        print_result(
            value,
            args,
            context=None if args.action in {'backup', 'restore', 'publish', 'fetch'} else 'account',
        )
        return 0
    signer_override = private_identity_key(args.key) if args.key else None
    state = ClientState(
        args.config_dir,
        server=args.server,
        profile=args.profile,
        migrate_from=args.migrate_from,
        account=args.account,
    )
    transport_options = {'endpoint': args.endpoint} if args.endpoint is not None else {}
    transport = TRANSPORTS[args.transport](state.server, **transport_options)
    client = MsgClient(state, transport)
    if signer_override is not None:
        state.signer = signer_override
        client.signer_override = state.signer
        # Key override is invocation-only, never written back to the primary identity.
        state.data.pop('token', None)
    if args.certificate:
        state.data['certificates'] = list(dict.fromkeys([*state.certificates, *args.certificate]))
    if args.as_subject:
        state.data['subject_id'] = args.as_subject
    try:
        command = args.command
        if command in {'agent', 'listen'}:
            from msg.client_agent_cli import run_remote

            return await run_remote(client, args)
        if args.user:
            if command == 'identity' and args.action == 'new':
                require(args.handle == args.user, 'connection_user_mismatch')
            elif command != 'login':
                await client.require_username(args.user)
        if command == 'mount':
            from msg.client_mount import run_mount

            return await run_mount(client, args)
        if command in {'login', 'logout', 'auth'}:
            from msg.client_oauth import run_command

            result = await run_command(client, args)
            if args.user and command == 'login':
                await client.require_username(args.user)
        elif command == 'internet':
            from msg.client_internet import run_command

            result = await run_command(client, args)
        elif command == 'yubikey':
            from msg.client_yubikey import run_command

            result = await run_command(client, args)
        elif command == 'api-key':
            from msg.client_api_keys import run_command

            result = await run_command(client, args, arguments)
        elif command == 'identity':
            if args.action.startswith('delegated-'):
                from msg.client_delegated import run_command

                result = await run_command(client, args)
            elif args.action == 'new':
                result = await client.register(args.handle)
            elif args.action == 'rename':
                result = await client.rename_identity(args.handle)
            elif args.action == 'temporary':
                result = await client.temporary()
            elif args.action == 'rotate-token':
                result = await client.rotate_token()
            elif args.action == 'recover-token':
                result = await client.recover_token()
            elif args.action == 'upgrade':
                result = await client.upgrade(args.handle)
            elif args.action == 'custodial':
                result = await client.custodial(args.handle)
            elif args.action == 'upgrade-custodial':
                result = await client.upgrade_custodial(
                    args.handle, external_ciphertexts_migrated=args.external_history == 'migrated'
                )
            elif args.action == 'migration':
                from msg.client_custodial import inventory, migrate, transition

                if args.migration_action == 'status':
                    result = await inventory(client)
                elif args.migration_action == 'migrate':
                    result = await migrate(client, method=args.method, limit=args.limit)
                elif args.migration_action == 'finalize':
                    result = await transition(
                        client,
                        'finalize',
                        resolution=args.resolution,
                        loss_revisions=args.loss_revision,
                        reason=args.reason,
                        external_ciphertexts_migrated=args.external_history == 'migrated',
                    )
                else:
                    result = await transition(
                        client,
                        'recovery_envelope' if args.migration_action == 'envelope' else 'refresh',
                    )
            else:
                from msg.client_accounts import identity_status
                from msg.client_upgrade import pending_upgrade

                result = identity_status(state, signer_override=client.signer_override)
                if state.data.get('delegated_identity'):
                    result['delegated_identity'] = state.data['delegated_identity']
                pending = pending_upgrade(state)
                if pending is not None:
                    result['pending_upgrade'] = pending
        elif command == 'call':
            expected = []
            for item in args.expect:
                rid, sep, generation = item.rpartition('=')
                require(sep and generation.isdecimal(), 'invalid_expected_generation')
                expected.append((rid, int(generation)))
            params = arguments(args.arguments)
            require(args.contract_version > 0, 'invalid_operation_version')
            result = None
            formatted = args.jq is not None or args.template is not None
            if formatted and args.json_fields is None and 'cursor' in params:
                from msg.client_output import cursor_output

                require(not args.return_field, 'json_query_conflict')
                result = await cursor_output(client, args.operation, args.contract_version, params)
            else:
                require(not formatted or args.json_fields not in {None, ''}, 'json_fields_required')
            if args.json_fields is not None:
                from msg.client_output import json_fields

                require(not args.return_field, 'json_query_conflict')
                params, result = await json_fields(
                    client, args.operation, args.contract_version, params, args.json_fields
                )
            if result is None:
                result = await client.call(
                    args.operation,
                    params,
                    request_id=args.request_id,
                    expected=expected,
                    return_fields=args.return_field,
                    contract_version=args.contract_version,
                )
        elif command in {'money', 'store', 'bounty', 'orders', 'delivery'}:
            from msg.client_market import run_command

            result = await run_command(client, args, arguments)
        elif command == 'operations':
            result = await client.call('discovery.operations')
        elif command == 'schema':
            result = await client.call('discovery.schema', {'operation': args.operation})
        elif command == 'resolve':
            params = {'address': args.address}
            if args.revision:
                params['revision'] = args.revision
            result = await client.call('discovery.resolve', params)
        elif command == 'events':
            params = {
                name: getattr(args, name)
                for name in ('resource', 'cursor', 'limit')
                if getattr(args, name) is not None
            }
            result = await client.call('communication.events', params)
        elif command == 'read':
            from msg.core.addressing import parse_address

            target, pinned = parse_address(args.resource, state.server)
            require(
                pinned is None or args.revision is None or pinned == args.revision,
                'revision_mismatch',
            )
            params = {'id': target}
            if args.revision or pinned:
                params['revision'] = args.revision or pinned
            if args.field:
                params['fields'] = args.field
            if args.meta:
                params['view'] = 'meta'
            result = await client.call('discovery.get', params)
            if args.ack and result.status == 'ok':
                require(result.data.get('revision'), 'ack_revision_required')
                ack = await client.ack(
                    ResourceRef(id=result.data['id'], revision=result.data['revision'])
                )
                result = {'read': result_wire(result), 'ack': result_wire(ack)}
        elif command in {'follow', 'unfollow', 'follows', 'followers'}:
            target = args.subject or state.subject
            require(target is not None, 'authentication_required')
            if not target.startswith(('/', 'u_')):
                target = '/@' + target.removeprefix('@')
            if command in {'follow', 'unfollow'}:
                result = await client.call('communication.' + command, {'id': target})
            else:
                params = {'subject_id': target, 'limit': args.limit}
                if args.after:
                    params['after'] = args.after
                operation = 'agent_following' if command == 'follows' else 'followers'
                result = await client.call('communication.' + operation, params)
        elif command == 'feed':
            result = await client.call(
                'discovery.recommendations',
                {
                    'limit': args.limit,
                    'interests': args.interest,
                },
            )
        elif command == 'following':
            if args.cursor:
                require(args.limit is None, 'cursor_query_mismatch')
                params = {'cursor': args.cursor}
            else:
                limit = 20 if args.limit is None else args.limit
                require(1 <= limit <= 100, 'invalid_limit')
                params = {'limit': limit}
            result = await client.call('communication.following', params)
        elif command == 'search':
            require(1 <= args.limit <= 100, 'invalid_search_limit')
            extended = (
                'source_kind',
                'source_version',
                'revision',
                'relation_type',
                'relation_to',
                'relation_from',
                'fields',
                'facets',
                'has_replies',
                'has_references',
            )
            if args.cursor:
                require(args.scope is None and args.terms is None, 'cursor_query_mismatch')
                require(
                    args.limit == 50
                    and args.mode == 'all'
                    and args.field == 'all'
                    and args.exact is None
                    and args.not_terms is None
                    and args.type is None
                    and args.owner is None
                    and args.author is None
                    and args.tag is None
                    and args.state is None
                    and args.created_after is None
                    and args.created_before is None
                    and args.updated_after is None
                    and args.updated_before is None
                    and args.has_attachment is None
                    and args.depth is None
                    and not args.no_recursive
                    and args.order is None
                    and not args.no_snippet
                    and not args.explain
                    and not args.suggest
                    and not args.spell
                    and all(getattr(args, key) is None for key in extended),
                    'cursor_query_mismatch',
                )
                params = {'cursor': args.cursor}
                # Inspect only bounded public query metadata to choose a wire
                # contract. The server verifies the MAC, principal and grants.
                require(len(args.cursor) <= 8192 and args.cursor.count('.') == 1, 'invalid_cursor')
                cursor_body = loads(unb64(args.cursor.split('.')[0], limit=8192))
                require(
                    isinstance(cursor_body, dict)
                    and cursor_body.get('kind') == 'read-page'
                    and isinstance(cursor_body.get('query'), dict),
                    'invalid_cursor',
                )
                query = cursor_body['query']
                require(
                    query.get('operation') == 'discovery.lexical_search'
                    and isinstance(query.get('arguments'), dict),
                    'invalid_cursor',
                )
                version = search_query_version(query['arguments'])
            else:
                require(
                    args.scope is not None and (args.terms or args.exact), 'search_query_required'
                )
                params = {
                    'scope': args.scope,
                    'mode': args.mode,
                    'field': args.field,
                    'limit': args.limit,
                }
                if args.scope.startswith('{'):
                    params['scope'] = json_input(args.scope)
                if args.terms:
                    params['terms'] = args.terms
                for key in (
                    'exact',
                    'not_terms',
                    'type',
                    'owner',
                    'author',
                    'tag',
                    'state',
                    'created_after',
                    'created_before',
                    'updated_after',
                    'updated_before',
                    'depth',
                    'order',
                    'source_kind',
                    'source_version',
                    'revision',
                    'relation_type',
                    'relation_to',
                    'relation_from',
                    'facets',
                ):
                    value = getattr(args, key)
                    if value is not None:
                        params[key] = value
                for key in ('has_attachment', 'has_replies', 'has_references'):
                    if getattr(args, key) is not None:
                        params[key] = getattr(args, key)
                if args.fields is not None:
                    params['fields'] = args.fields.split(',')
                if args.suggest:
                    params['suggest'] = True
                if args.spell:
                    params['spell'] = True
                if args.no_recursive:
                    params['recursive'] = False
                if args.no_snippet:
                    params['snippet'] = False
                if args.explain:
                    params['explain'] = 'compact'
                version = search_query_version(params)
            result = await client.call('discovery.lexical_search', params, contract_version=version)
        elif command == 'grep':
            require(0 <= args.before <= 3 and 0 <= args.after <= 3, 'invalid_grep_context')
            require(
                1 <= args.max_matches <= 100 and 1 <= args.max_files <= 100, 'invalid_grep_limit'
            )
            params = {
                'scope': args.scope,
                'pattern': args.pattern,
                'regex': args.regex,
                'case_sensitive': not args.ignore_case,
                'before': args.before,
                'after': args.after,
                'max_matches': args.max_matches,
                'max_files': args.max_files,
            }
            for key in ('glob', 'exclude_glob'):
                value = getattr(args, key)
                if value is not None:
                    params[key] = value
            if args.files_with_matches:
                params['files_with_matches'] = True
            if args.count_only:
                params['count_only'] = True
            result = await client.call('discovery.grep', params)
        elif command == 'post':
            if args.file:
                uploaded = await client.upload(args.file, media_type='text/markdown')
                params = {'parent': args.topic, 'source': wire(uploaded.output)}
            else:
                params = {'parent': args.topic, 'body': args.text}
            params.update({
                k: getattr(args, k) for k in ('name', 'summary') if getattr(args, k) is not None
            })
            result = await client.call(
                'content.post_create', params, contract_version=2 if args.summary is not None else 1
            )
        elif command == 'reply':
            result = await client.call(
                'discussion.reply',
                {
                    'target': {'id': args.resource},
                    'body': args.text,
                    **{
                        k: getattr(args, k)
                        for k in ('name', 'summary')
                        if getattr(args, k) is not None
                    },
                },
                contract_version=2 if args.summary is not None else 1,
            )
        elif command == 'dm':
            if args.action == 'list':
                result = await client.call('communication.dm_list')
            elif args.action == 'request':
                result = await client.call(
                    'communication.dm_request',
                    {
                        'recipient': args.recipient,
                        **({'introduction': args.intro} if args.intro is not None else {}),
                    },
                    contract_version=2 if args.intro is not None else 1,
                )
            elif args.action == 'send':
                result = await client.call(
                    'communication.dm_send',
                    {'conversation_id': args.conversation, 'body': args.text},
                )
            elif args.action == 'read':
                result = await client.call('discovery.get', {'id': args.resource})
            elif args.action == 'block':
                result = await client.call('communication.dm_block', {'subject_id': args.subject})
            else:
                result = await client.call(
                    'communication.dm_' + args.action, {'conversation_id': args.conversation}
                )
        elif command in {'request', 'offer', 'checkpoint', 'proposal', 'watch'}:
            from msg.client_collaboration import run_command as run_collaboration_command

            result = await run_collaboration_command(client, args)
        elif command == 'handoff':
            if args.action == 'create':
                params = {'to_subject': args.recipient, 'resource_refs': args.ref}
                if args.message is not None:
                    params['message'] = args.message
                if args.next_action is not None:
                    params['next_action'] = args.next_action
                if args.capsule:
                    params['capsule'] = loads(Path(args.capsule).read_text())
                result = await client.call(
                    'communication.handoff_create',
                    params,
                    contract_version=2 if args.capsule else 1,
                )
            elif args.action == 'list':
                params = {}
                if args.limit is not None:
                    params['limit'] = args.limit
                if args.after is not None:
                    params['after'] = args.after
                result = await client.call('communication.handoff_list', params)
            elif args.action == 'get':
                result = await client.call('communication.handoff_get', {'id': args.id})
            else:
                result = await client.call(
                    'communication.handoff_decide',
                    {
                        'id': args.id,
                        'decision': args.action,
                        'expected_generation': args.generation,
                    },
                )
        elif command == 'lease':
            if args.action == 'acquire':
                result = await client.call(
                    'communication.lease_acquire',
                    {'target': args.target, 'purpose': args.purpose, 'expires_at': args.expires_at},
                )
            elif args.action == 'list':
                params = {}
                if args.limit is not None:
                    params['limit'] = args.limit
                if args.after is not None:
                    params['after'] = args.after
                result = await client.call('communication.lease_list', params)
            elif args.action == 'get':
                result = await client.call('communication.lease_get', {'id': args.id})
            elif args.action == 'renew':
                result = await client.call(
                    'communication.lease_renew',
                    {
                        'id': args.id,
                        'expires_at': args.expires_at,
                        'expected_generation': args.generation,
                    },
                )
            else:
                result = await client.call(
                    'communication.lease_release',
                    {'id': args.id, 'expected_generation': args.generation},
                )
        elif command == 'fork':
            params = {'target': {'id': args.resource, 'revision': args.revision}, 'body': args.body}
            if args.parent:
                params['parent'] = args.parent
            result = await client.call('discussion.fork', params)
        elif command == 'prove-reading':
            ranges = None
            if args.part or args.lines:
                ranges = []
                for part in args.part or args.lines:
                    pieces = part.split(':')
                    require(
                        len(pieces) == 2 and all(p.isascii() and p.isdigit() for p in pieces),
                        'reading_range_invalid',
                    )
                    start, end = map(int, pieces)
                    require(
                        1 <= start <= end if args.lines else start < end, 'reading_range_invalid'
                    )
                    ranges.append({'start': start, 'end': end})
            result = await client.prove_reading(
                ResourceRef(id=args.resource, revision=args.revision),
                None if args.lines else ranges,
                args.note,
                line_ranges=ranges if args.lines else None,
            )
        elif command == 'readings':
            params = {'id': args.resource, 'limit': args.limit}
            if args.revision:
                params['revision'] = args.revision
            if args.cursor:
                params['cursor'] = args.cursor
            result = await client.call('discussion.readings', params)
        elif command == 'prove':
            result = await client.prove(
                ResourceRef(id=args.resource, revision=args.revision), args.kind, args.note
            )
        elif command == 'ack':
            result = await client.ack(ResourceRef(id=args.resource, revision=args.revision))
        elif command == 'upload':
            result = await client.upload(
                args.file,
                transfer_id=args.resume,
                part_bytes=args.part_bytes,
                media_type=args.media_type,
                target=args.target,
            )
        elif command == 'download':
            result = await client.download(
                ResourceRef(id=args.resource, revision=args.revision),
                args.file,
                part_bytes=args.part_bytes,
            )
        elif command == 'cert':
            from msg.client_certificates import issue_certificate, request_certificate

            if args.action == 'renew':
                result = await client.renew_certificate()
            elif args.action == 'get':
                result = await client.call('cert.get', {'id': args.id})
            elif args.action == 'request':
                result = await request_certificate(client, arguments(args.spec), args.ca_key)
            else:
                result = await issue_certificate(
                    client,
                    args.csr_id,
                    args.issuer,
                    args.ca_key,
                    expected_digest=args.approve_digest,
                )
        elif command == 'keystore':
            from msg.client_secrets import get_secret, put_secret

            if args.action == 'put':
                result = await put_secret(client, args.name, args.file, args.recipient)
            elif args.action == 'get':
                result = await get_secret(client, args.resource, args.output, args.private_key)
            else:
                result = await client.call('keystore.list')
        elif command == 'recovery':
            from msg.client_recovery import rewrap_age_keystore_entry, save_recovery_envelope
            from msg.security.age_keys import encryption_key_id, public_from_recipient

            if args.action == 'policy':
                result = await client.call('identity.recovery_policy_get')
            elif args.action == 'policy-set':
                require(args.clear or bool(args.recipient), 'recovery_recipients_required')
                require(not (args.clear and args.recipient), 'ambiguous_recovery_policy')
                require(state.encryption_recipient is not None, 'encryption_key_not_found')
                current = client.checked(await client.call('identity.recovery_policy_get'))
                result = await client.call(
                    'identity.recovery_policy_set',
                    {
                        'expected_version': current.data['version'],
                        'encryption_key_id': encryption_key_id(
                            public_from_recipient(state.encryption_recipient)
                        ),
                        'recipients': []
                        if args.clear
                        else [{'recipient': value} for value in args.recipient],
                    },
                )
            elif args.action == 'backup':
                stored, envelope, metadata = await save_recovery_envelope(
                    client, args.recipient, policy_version=args.policy_version, name=args.name
                )
                result = {
                    'keystore': result_wire(stored),
                    'envelope': result_wire(envelope),
                    'metadata': metadata,
                }
            elif args.action == 'list':
                result = await client.call('identity.recovery_envelope_list')
            elif args.action == 'get':
                result = await client.call('identity.recovery_envelope_get', {'id': args.id})
            else:
                result = await rewrap_age_keystore_entry(
                    client,
                    args.resource,
                    args.old_identity,
                    expected_revision=args.expected_revision,
                )
        elif command == 'legacy':
            if args.action in {'put', 'archive'}:
                payload = arguments(args.spec) if args.action == 'put' else {}
                expected = ()
                if args.action == 'archive' or 'expected_revision' in payload:
                    require(state.subject is not None, 'subject_required')
                    current = client.checked(
                        await client.call('identity.legacy_get', {'subject_id': state.subject})
                    )
                    if 'expected_revision' in payload:
                        require(
                            current.data['revision'] == payload['expected_revision'],
                            'revision_conflict',
                        )
                    meta = client.checked(
                        await client.call(
                            'discovery.get', {'id': current.data['id'], 'view': 'meta'}
                        )
                    )
                    expected = ((current.data['id'], meta.data['generation']),)
                operation = (
                    'identity.legacy_put' if args.action == 'put' else 'identity.legacy_archive'
                )
                result = await client.call(
                    operation,
                    payload,
                    expected=expected,
                    contract_version=args.contract_version if args.action == 'put' else 1,
                )
            elif args.action == 'status':
                result = await client.call('identity.legacy_status')
            else:
                subject = args.subject or state.subject
                require(subject is not None, 'subject_required')
                params = {'subject_id': subject}
                if args.revision:
                    params['revision'] = args.revision
                result = await client.call('identity.legacy_get', params)
        elif command == 'hosting':
            if args.action == 'create':
                require(
                    state.signer is not None and state.token is None, 'signing_identity_required'
                )
                result = await client.call(
                    'hosting.create', {'parent': args.parent, 'name': args.name}
                )
            elif args.action in {'preview', 'deploy'}:
                entries = json_input(args.entries)
                if args.action == 'preview':
                    result = await client.hosting_preview(args.website, entries)
                else:
                    result = await client.hosting_deploy(args.website, entries)
            elif args.action == 'activate':
                result = await client.hosting_activate(args.website, args.revision)
            elif args.action == 'history':
                result = await client.call('discovery.get', {'id': args.website, 'view': 'history'})
            else:
                # A downloaded HTML/SVG file has no server CSP when opened from
                # disk. Keep the CLI's preview artifact inert by construction.
                require(
                    args.output.suffix.lower() in {'.txt', '.bin'}, 'inert_preview_output_required'
                )
                data, metadata = await client.hosting_preview_get(
                    args.site_path, args.candidate_id, args.file_path
                )
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, 'O_NOFOLLOW'):
                    flags |= os.O_NOFOLLOW
                fd = os.open(args.output, flags, 0o600)
                try:
                    with os.fdopen(fd, 'wb') as output:
                        output.write(data)
                except BaseException:
                    args.output.unlink(missing_ok=True)
                    raise
                result = {
                    'status': 'ok',
                    'output': str(args.output),
                    'bytes': len(data),
                    **metadata,
                }
        elif command == 'mcp':
            from msg.transports.stdio import serve_stdio

            await serve_stdio(client)
            return 0
        elif command == 'tui':
            from msg.tui import run_tui

            await run_tui(client)
            return 0
        else:
            raise Failure('unknown_command')
        rendered = result_wire(result) if isinstance(result, OperationResult) else result
        if (
            command == 'hosting'
            and args.action == 'preview'
            and isinstance(rendered, dict)
            and rendered.get('status') == 'ok'
        ):
            rendered['data'].pop('url', None)
        if command == 'call' and not (
            isinstance(result, OperationResult) and result.status != 'ok'
        ):
            from msg.client_output import render_jq, render_template

            if args.jq is not None:
                print(await render_jq(args.jq, rendered), end='')
            elif args.template is not None:
                print(render_template(args.template, rendered))
            else:
                # `call` is the raw operation interface; field selection and
                # custom formatters retain their machine-readable behavior.
                print_result(rendered, args, raw=args.json_fields is not None)
        else:
            print_result(
                rendered,
                args,
                context='auth_approval'
                if command == 'auth' and args.action in {'approve', 'deny'}
                else None,
                identity={
                    'account': state.account,
                    'handle': state.data.get('handle'),
                    'subject_id': state.subject,
                    'server': state.server,
                },
            )
        return 1 if isinstance(result, OperationResult) and result.status == 'error' else 0
    finally:
        await transport.close()


def main(argv=None):
    from msg.client_display import print_result

    args = None
    try:
        from msg.client_connection import expand_connection_args

        argument_parser = parser()
        commands = next(
            action.choices
            for action in argument_parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        args = argument_parser.parse_args(
            expand_connection_args(sys.argv[1:] if argv is None else argv, commands)
        )
        return asyncio.run(run(args))
    except Failure as exc:
        print_result({'status': 'error', 'error': exc.as_dict()}, args, stream=sys.stderr)
        if args is not None and args.command == 'internet' and sys.stderr.isatty():
            from msg.client_internet import retry_hint

            hint = retry_hint(exc.as_dict())
            if hint:
                print(hint, file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        # File contents and credential-bearing URLs never appear in errors.
        print_result(
            {
                'status': 'error',
                'error': {'code': 'local_io_error', 'type': type(exc).__name__},
            },
            args,
            stream=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
