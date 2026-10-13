"""Local agent labels and JSONL listeners reuse one existing account."""

from __future__ import annotations

import asyncio
import math
import sys
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
    for name in ('create', 'archive', 'show'):
        command = actions.add_parser(name)
        command.add_argument('name')
        command.add_argument('--remote', action='store_true')
    reading = actions.add_parser('read', help='Read one known message by ID.')
    reading.add_argument('message_id')
    reading.add_argument('name', nargs='?', help='Subagent name (or use --agent).')
    reading.add_argument('--remote', action='store_true')
    listing = actions.add_parser('list')
    listing.add_argument('--remote', action='store_true')
    listing.add_argument('--active', action='store_true', help='Show only active subagents.')
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
    inbox.add_argument(
        '--tail', action='store_true', help='Start at current tail without scanning history.'
    )
    inbox.add_argument('--sender', '--from', dest='sender', help='Filter messages by sender label.')
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
    listener.add_argument(
        '--subscribe',
        action='store_true',
        help='Use authorized server event waits (events@2); no mailbox or offline fallback.',
    )
    listener.add_argument('--resource', help='With --subscribe, limit events to this resource.')
    listener.add_argument('--cursor')
    listener.add_argument('--cursor-file', type=Path, help='Independent durable listener position.')
    listener.add_argument('--interval', type=float, default=1.0)
    listener.add_argument(
        '--wait-mode',
        choices=('auto', 'events', 'poll'),
        default='auto',
        help='Auto: local native events or remote signed bounded waits, with explicit polling fallback. '
        'Events: require native/signed waits. Poll: fixed-interval legacy behavior.',
    )
    listener.add_argument(
        '--max-idle-interval',
        type=float,
        default=30.0,
        help='Remote auto mode: maximum idle polling delay in seconds (default: 30).',
    )
    listener.add_argument(
        '--from-now', action='store_true', help='Skip existing events on a new listener.'
    )
    listener.add_argument(
        '--once', action='store_true', help='Drain currently available events and exit.'
    )
    listener.add_argument(
        '--max-events',
        type=int,
        help='Exit after this many matching events; without --once, 1 waits for one event.',
    )
    listener.add_argument(
        '--status-json',
        action='store_true',
        help='Emit transition-only status JSONL on stderr (no bodies or cursors). '
        'Status is not an ACK or task completion.',
    )
    listener.add_argument(
        '--max-pages',
        type=int,
        help='With --once, bound source pages; save the cursor for the next invocation.',
    )


def local_command(args):
    if args.command == 'listen':
        require(
            not getattr(args, 'resource', None) or getattr(args, 'subscribe', False),
            'subscription_required',
        )
    if getattr(args, 'subscribe', False):
        return False
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


async def stream(
    state,
    args,
    fetch,
    source,
    *,
    wait_for_change=None,
    resolve_legacy=None,
    server_wait=False,
    wait_fetch=None,
):
    from msg.client_listener import ListenerStatus, listen

    status = getattr(args, '_listener_status', None)
    if status is None and getattr(args, 'status_json', False):
        status = ListenerStatus()
    remote = not source.startswith('local:')
    idle_limit = args.interval
    if remote and getattr(args, 'wait_mode', 'auto') == 'auto':
        maximum = getattr(args, 'max_idle_interval', 30.0)
        require(math.isfinite(maximum) and maximum > 0, 'invalid_listener_interval')
        idle_limit = max(args.interval, maximum)
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
        wait_for_change=wait_for_change,
        max_idle_interval=idle_limit,
        cursor_only=remote,
        resolve_legacy=resolve_legacy,
        server_wait=server_wait,
        wait_fetch=wait_fetch,
        status=status,
    )
    return 0


async def run_local(state, args):
    from msg.client_subagents import LocalAgents

    require(not getattr(args, 'receipt', False), 'receipt_requires_remote')
    store = LocalAgents(state, username=args.username or args.user)
    try:
        if args.command == 'listen':
            if getattr(args, 'status_json', False):
                from msg.client_listener import ListenerStatus

                args._listener_status = ListenerStatus()
            name = inbox_name(args)

            async def fetch(cursor, tail=False):
                return await asyncio.to_thread(store.inbox, name, cursor=cursor, tail=tail)

            mode = getattr(args, 'wait_mode', 'auto')
            if mode != 'poll' and not args.once:
                from msg.client_events import EventsUnavailable, LocalEvents

                try:
                    events = LocalEvents(store.path)
                except EventsUnavailable, NotImplementedError:
                    if mode == 'events':
                        raise Failure('listener_events_unavailable') from None
                    if getattr(args, '_listener_status', None) is not None:
                        args._listener_status(
                            'polling_fallback', reason='native_events_unavailable'
                        )
                    else:
                        print(
                            'msg listen: native events unavailable; using polling.',
                            file=sys.stderr,
                            flush=True,
                        )
                else:
                    with events:
                        return await stream(
                            state,
                            args,
                            fetch,
                            'local:' + store.username,
                            wait_for_change=events.wait,
                        )
            return await stream(state, args, fetch, 'local:' + store.username)
        if args.action == 'create':
            result = store.create(args.name)
        elif args.action == 'show':
            result = store.show(args.name)
        elif args.action == 'read':
            result = store.read(inbox_name(args), args.message_id)
        elif args.action == 'archive':
            result = store.archive(args.name)
        elif args.action == 'list':
            result = store.list()
            if getattr(args, 'active', False):
                result = [
                    item for item in result if item.get('active', not item.get('archived', False))
                ]
        elif args.action == 'send':
            require(bool(args.agent), 'agent_required')
            result = store.send(
                args.agent, args.recipient, message_text(args), message_id=args.message_id
            )
        else:
            result = store.inbox(
                inbox_name(args),
                cursor=args.cursor,
                limit=args.limit,
                tail=getattr(args, 'tail', False),
                sender=getattr(args, 'sender', None),
            )
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
    if args.command == 'listen' and getattr(args, 'subscribe', False):
        from msg.client_subscription import Subscription

        require(not args.agent, 'subscription_requires_account_reader')
        source = Subscription(
            client,
            resource=args.resource,
            once=args.once,
            interval=args.interval,
        )
        return await stream(
            client.state,
            args,
            source.fetch,
            'subscription:' + (args.resource or ''),
            server_wait=True,
        )
    require(not getattr(args, 'resource', None), 'subscription_required')
    changes_wait = None
    if args.command == 'listen':
        from msg.client_changes_wait import ChangesWait

        if getattr(args, 'status_json', False):
            from msg.client_listener import ListenerStatus

            args._listener_status = ListenerStatus()
        changes_wait = ChangesWait(
            client,
            mode=getattr(args, 'wait_mode', 'auto'),
            once=args.once,
            interval=args.interval,
            max_idle_interval=getattr(args, 'max_idle_interval', 30),
            status=getattr(args, '_listener_status', None),
        )
    if args.command == 'listen' and not args.agent:

        async def fetch(cursor, tail=False, wait=False):
            current = cursor
            while True:
                params = {'limit': 50}
                if current:
                    params['cursor'] = current
                page = await changes_wait.read(params, wait=wait and not tail)
                wait = False
                items = page['items']
                if tail and isinstance(page.get('tail_cursor'), str):
                    return {'items': [], 'cursor': page['tail_cursor'], 'has_more': False}
                current = page['sync_cursor']
                more = page.get('has_more', len(items) == 50)
                if not tail or not more:
                    return {
                        'items': [] if tail else items,
                        'cursor': current,
                        'has_more': more,
                    }

        async def resolve_legacy(message_id):
            current = await client.call('communication.event', {'id': str(message_id)})
            if current.status != 'ok' and current.error.code == 'not_found':
                return None
            event = wire(client.checked(current).data)
            return {
                **event,
                'type': event['operation'],
                'data': {'operation': event['operation']},
                'resources': [address['ref'] for address in event['resources']],
            }

        return await stream(
            client.state,
            args,
            fetch,
            'account-events',
            resolve_legacy=resolve_legacy,
            wait_fetch=(lambda cursor, tail=False: fetch(cursor, tail=tail, wait=True))
            if changes_wait.enabled
            else None,
        )
    from msg.client_subagents_remote import RemoteAgents

    store = RemoteAgents(client, username=args.username)
    if args.command == 'listen':
        name = inbox_name(args)

        async def fetch(cursor, tail=False, wait=False):
            return await store.inbox(
                name,
                cursor=cursor,
                tail=tail,
                changes_reader=changes_wait.read,
                wait=wait,
            )

        return await stream(
            client.state,
            args,
            fetch,
            'remote',
            resolve_legacy=lambda message_id: store.read(name, message_id),
            wait_fetch=(lambda cursor, tail=False: fetch(cursor, tail=tail, wait=True))
            if changes_wait.enabled
            else None,
        )
    if args.action == 'create':
        result = await store.create(args.name)
    elif args.action == 'show':
        result = await store.show(args.name)
    elif args.action == 'read':
        result = await store.read(inbox_name(args), args.message_id)
    elif args.action == 'archive':
        result = await store.archive(args.name)
    elif args.action == 'list':
        result = await store.list()
        if getattr(args, 'active', False):
            result = [
                item for item in result if item.get('active', not item.get('archived', False))
            ]
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
        result = await store.inbox(
            inbox_name(args),
            cursor=args.cursor,
            limit=args.limit,
            tail=getattr(args, 'tail', False),
            sender=getattr(args, 'sender', None),
        )
    print(canonical({'status': 'ok', 'data': result}).decode(), flush=True)
    return 0
