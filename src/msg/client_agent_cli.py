"""Local agent labels and JSONL listeners reuse one existing account."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from msg.client import ClientState
from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.paths import ClientPaths


def add_commands(commands):
    agents = commands.add_parser(
        'agent', help='Private subagents sharing one account; local by default.'
    )
    actions = agents.add_subparsers(dest='action', required=True)
    for name in ('create', 'archive'):
        command = actions.add_parser(name)
        command.add_argument('name')
        command.add_argument('--remote', action='store_true')
    listing = actions.add_parser('list')
    listing.add_argument('--remote', action='store_true')
    sending = actions.add_parser('send')
    sending.add_argument('recipient')
    sending.add_argument('message', nargs='?')
    source = sending.add_mutually_exclusive_group()
    source.add_argument('--text')
    source.add_argument('--file', type=Path)
    sending.add_argument('--message-id', help='Reuse this ID when retrying a send.')
    sending.add_argument('--remote', action='store_true')
    sending.add_argument(
        '--receipt',
        action='store_true',
        help='With --remote, return the saved message reference without echoing its body.',
    )
    inbox = actions.add_parser('inbox')
    inbox.add_argument('name', nargs='?')
    inbox.add_argument('--cursor')
    inbox.add_argument('--limit', type=int, default=50)
    inbox.add_argument('--remote', action='store_true')
    listener = commands.add_parser(
        'listen', help='Wait for events; output flushed JSONL until stopped.'
    )
    listener.add_argument(
        '--remote',
        action='store_true',
        help='With --agent, use the account private server mailbox.',
    )
    listener.add_argument(
        '--event',
        action='append',
        default=[],
        help='Exact event type; repeat to include more types.',
    )
    listener.add_argument('--cursor')
    listener.add_argument('--cursor-file', type=Path, help='Independent durable listener position.')
    listener.add_argument('--interval', type=float, default=1.0)
    listener.add_argument(
        '--from-now', action='store_true', help='Skip existing events on a new listener.'
    )
    listener.add_argument(
        '--once', action='store_true', help='Drain currently available events and exit.'
    )
    listener.add_argument('--max-events', type=int, help='Exit after this many matching events.')
    listener.add_argument(
        '--max-pages',
        type=int,
        help='With --once, bound source pages; save the cursor for the next invocation.',
    )


def local_command(args):
    return (args.command == 'agent' or args.command == 'listen' and args.agent) and not args.remote


def local_state(args):
    require(not args.remote, 'offline_remote_conflict')
    require(not getattr(args, 'receipt', False), 'receipt_requires_remote')
    try:
        return ClientState(
            args.config_dir,
            server=args.server,
            profile=args.profile,
            migrate_from=args.migrate_from,
            account=getattr(args, 'account', None),
        )
    except Failure as exc:
        if exc.code != 'server_required' or not (args.username or args.user):
            raise
        paths = ClientPaths.discover(
            args.config_dir, profile=args.profile, account=getattr(args, 'account', None)
        )
        paths.prepare()
        return SimpleNamespace(
            paths=paths, directory=paths.state, server='local', subject=None, data={}
        )


def message_text(args):
    require(
        sum(value is not None for value in (args.message, args.text, args.file)) == 1,
        'one_message_source_required',
    )
    if args.file is not None:
        with args.file.open('rb') as stream:
            value = stream.read(65537)
        require(len(value) <= 65536, 'agent_message_too_large')
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError:
            raise Failure('invalid_message_encoding') from None
    return args.text if args.text is not None else args.message


def inbox_name(args):
    name = getattr(args, 'name', None)
    require(bool(args.agent or name), 'agent_required')
    require(not (args.agent and name and args.agent != name), 'ambiguous_agent')
    return args.agent or name


def cursor_path(state, args, source):
    if args.cursor_file:
        return args.cursor_file
    from msg.paths import private_directory

    context = {
        'server': state.server,
        'subject': state.subject,
        'source': source,
        'agent': args.agent,
        'events': sorted(set(args.event)),
    }
    key = sha256(canonical(context)).hexdigest()[:24]
    return private_directory(state.paths.state / 'agent-listeners') / (key + '.json')


async def stream(state, args, fetch, source):
    from msg.client_listener import listen

    await listen(
        fetch,
        context={
            'server': state.server,
            'subject': state.subject,
            'source': source,
            'agent': args.agent,
        },
        cursor_file=cursor_path(state, args, source),
        cursor=args.cursor,
        interval=args.interval,
        event_types=args.event,
        once=args.once,
        max_events=args.max_events,
        max_pages=getattr(args, 'max_pages', None),
        from_now=args.from_now,
    )
    return 0


async def run_local(state, args):
    from msg.client_subagents import LocalAgents

    require(not getattr(args, 'receipt', False), 'receipt_requires_remote')
    store = LocalAgents(state, username=args.username or args.user)
    try:
        if args.command == 'listen':
            name = inbox_name(args)

            async def fetch(cursor, tail=False):
                return await asyncio.to_thread(store.inbox, name, cursor=cursor, tail=tail)

            return await stream(state, args, fetch, 'local:' + store.username)
        if args.action == 'create':
            result = store.create(args.name)
        elif args.action == 'archive':
            result = store.archive(args.name)
        elif args.action == 'list':
            result = store.list()
        elif args.action == 'send':
            require(bool(args.agent), 'agent_required')
            result = store.send(
                args.agent, args.recipient, message_text(args), message_id=args.message_id
            )
        else:
            result = store.inbox(inbox_name(args), cursor=args.cursor, limit=args.limit)
        print(canonical({'status': 'ok', 'data': result}).decode(), flush=True)
        return 0
    finally:
        close = getattr(store, 'close', None)
        if close:
            close()


async def run_remote(client, args):
    require(not args.offline, 'offline_remote_conflict')
    if args.user:
        await client.require_username(args.user)
    if args.command == 'listen' and not args.agent:

        async def fetch(cursor, tail=False):
            current = cursor
            while True:
                params = {'limit': 50}
                if current:
                    params['cursor'] = current
                result = client.checked(await client.call('communication.changes', params))
                items = wire(result.data['items'])
                if tail and isinstance(result.data.get('tail_cursor'), str):
                    return {'items': [], 'cursor': result.data['tail_cursor'], 'has_more': False}
                current = result.data['sync_cursor']
                more = result.data.get('has_more', len(items) == 50)
                if not tail or not more:
                    return {
                        'items': [] if tail else items,
                        'cursor': current,
                        'has_more': more,
                    }

        return await stream(client.state, args, fetch, 'account-events')
    from msg.client_subagents_remote import RemoteAgents

    store = RemoteAgents(client, username=args.username)
    if args.command == 'listen':
        name = inbox_name(args)

        async def fetch(cursor, tail=False):
            return await store.inbox(name, cursor=cursor, tail=tail)

        return await stream(client.state, args, fetch, 'remote')
    if args.action == 'create':
        result = await store.create(args.name)
    elif args.action == 'archive':
        result = await store.archive(args.name)
    elif args.action == 'list':
        result = await store.list()
    elif args.action == 'send':
        require(bool(args.agent), 'agent_required')
        result = await store.send(
            args.agent,
            args.recipient,
            message_text(args),
            message_id=args.message_id,
            receipt=getattr(args, 'receipt', False),
        )
    else:
        result = await store.inbox(inbox_name(args), cursor=args.cursor, limit=args.limit)
    print(canonical({'status': 'ok', 'data': result}).decode(), flush=True)
    return 0
