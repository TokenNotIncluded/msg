"""CLI adapters for explicit work records; no autonomous workflow execution."""


def add_commands(commands):
    watches = commands.add_parser('watch').add_subparsers(dest='action', required=True)
    create = watches.add_parser('create')
    create.add_argument('target', nargs='?')
    create.add_argument('--query-ref')
    create.add_argument('--query-revision')
    create.add_argument('--event', action='append', required=True)
    create.add_argument('--expires-at')
    create.add_argument('--delivery', choices=['inbox'], default='inbox')
    watches.add_parser('list')
    for action in ('get', 'cancel'):
        watches.add_parser(action).add_argument('id')
    for kind in ('request', 'offer', 'checkpoint', 'proposal'):
        actions = commands.add_parser(kind).add_subparsers(dest='action', required=True)
        create = actions.add_parser('create')
        if kind == 'request':
            for name in ('title', 'description', 'requirements'):
                create.add_argument('--' + name, required=True)
            create.add_argument('--due-at')
        elif kind == 'offer':
            for name in ('description', 'scope', 'availability'):
                create.add_argument('--' + name, required=True)
            create.add_argument('--capability-hint')
        elif kind == 'checkpoint':
            create.add_argument('--summary', required=True)
            create.add_argument('--resume-hint')
            create.add_argument('--state-ref')
        else:
            for name in ('target', 'base-revision', 'content', 'content-revision', 'message'):
                create.add_argument('--' + name, required=True)
        if kind != 'proposal':
            create.add_argument('--ref', action='append', default=[])
            create.add_argument('--expires-at')
        actions.add_parser('get').add_argument('id')
        listing = actions.add_parser('list')
        listing.add_argument('--limit', type=int)
        listing.add_argument('--after')
        transitions = {
            'request': ('claim', 'fulfill', 'cancel'),
            'offer': ('withdraw',),
            'checkpoint': (),
            'proposal': ('accept', 'reject', 'withdraw'),
        }[kind]
        for action in transitions:
            child = actions.add_parser(action)
            child.add_argument('id')
            child.add_argument('--generation', type=int, required=True)
            if kind == 'proposal':
                child.add_argument('--proposal-revision', required=True)
                if action == 'accept':
                    child.add_argument('--base-revision', required=True)
                    child.add_argument('--target', required=True)
                    child.add_argument('--target-generation', type=int, required=True)


async def run_command(client, args):
    kind, action = args.command, args.action
    expected = ()
    if kind == 'watch':
        if action == 'create':
            from msg.core.errors import require

            require(bool(args.target) != bool(args.query_ref), 'watch_target_required')
            require(
                bool(args.query_ref) == bool(args.query_revision), 'watch_query_revision_required'
            )
            params = {'event_types': args.event, 'delivery': args.delivery}
            if args.query_ref:
                params['query_ref'] = {'id': args.query_ref, 'revision': args.query_revision}
            else:
                params['target'] = args.target
            if args.expires_at:
                params['expires_at'] = args.expires_at
        else:
            params = {} if action == 'list' else {'id': args.id}
        version = 2 if action == 'create' and args.query_ref else 1
        return await client.call('communication.watch_' + action, params, contract_version=version)
    if action == 'create':
        fields = {
            'request': ('title', 'description', 'requirements', 'due_at', 'expires_at'),
            'offer': ('description', 'scope', 'availability', 'capability_hint', 'expires_at'),
            'checkpoint': ('summary', 'resume_hint', 'state_ref', 'expires_at'),
            'proposal': ('target', 'base_revision', 'message'),
        }[kind]
        params = {name: getattr(args, name) for name in fields if getattr(args, name) is not None}
        if kind == 'proposal':
            params['content_ref'] = {'id': args.content, 'revision': args.content_revision}
        else:
            params['resource_refs'] = args.ref
    elif action == 'list':
        params = {
            name: getattr(args, name)
            for name in ('limit', 'after')
            if getattr(args, name) is not None
        }
    else:
        params = {'id': args.id}
        if action != 'get':
            expected = ((args.id, args.generation),)
            if kind == 'proposal':
                params['proposal_revision'] = args.proposal_revision
                if action == 'accept':
                    params['base_revision'] = args.base_revision
                    expected += ((args.target, args.target_generation),)
    return await client.call('communication.' + kind + '_' + action, params, expected=expected)
