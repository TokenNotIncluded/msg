"""Deterministic scheduling inside private collaboration request resources."""

from msg.core import task_graph
from msg.core.codec import canonical, decode, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, Relation, ResourceRef
from msg.plugins.collaboration import _safe_ref, _self
from msg.plugins.common import assert_generation, create_resource, new_id, revise_resource
from msg.plugins.schemas import IDENTIFIER, REF, obj

NAME = {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{1,80}$'}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
DEPENDENCIES = {'type': 'array', 'items': NAME, 'uniqueItems': True, 'maxItems': 256}
TASK_FIELDS = {'id': NAME, 'instruction': TEXT, 'dependencies': DEPENDENCIES}
TASK_INPUT = obj(TASK_FIELDS, ('id', 'instruction'))
TASK_RECORD = obj(
    {
        **TASK_FIELDS,
        'status': {'enum': ['pending', 'running', 'succeeded', 'failed', 'blocked']},
        'worker': {'type': 'string', 'maxLength': 256},
        'attempt': NAME,
        'lease_until': {'type': 'number'},
        'started_at': {'type': 'number'},
        'finished_at': {'type': 'number'},
        'result_slot': {'type': 'integer', 'minimum': 0, 'maximum': 255},
        'error': TEXT,
    },
    ('id', 'instruction', 'dependencies', 'status'),
)
CONCURRENCY = {'type': 'integer', 'minimum': 1, 'maximum': 64}
GRAPH = obj(
    {
        'concurrency': CONCURRENCY,
        'tasks': {'type': 'array', 'items': TASK_RECORD, 'minItems': 1, 'maxItems': 256},
    },
    ('concurrency', 'tasks'),
)
WORKERS = {'type': 'array', 'items': IDENTIFIER, 'minItems': 1, 'maxItems': 64, 'uniqueItems': True}
LEASE = {'type': 'integer', 'minimum': 1, 'maximum': 3600}
# Loss of resource access is not a transient failure or a reason to publish a result.
HIDDEN = {
    'permission_denied', 'credential_ceiling', 'certificate_gate', 'delegation_scope',
    'ancestor_inactive', 'not_found', 'collaboration_ref_forbidden',
}


def install(app, op):
    from msg.plugins.collaboration_resources import KINDS, _folder, _load, _project, _result

    @op(
        'communication.task_plan_create',
        obj(
            {
                'title': TEXT,
                'tasks': {'type': 'array', 'items': TASK_INPUT, 'minItems': 1, 'maxItems': 256},
                'concurrency': CONCURRENCY,
                'workers': WORKERS,
            },
            ('title', 'tasks', 'concurrency'),
        ),
        signature=True,
    )
    async def create(ctx, request, tx):
        subject = await _self(app, ctx, request, tx)
        args = request.arguments
        workers = args.get('workers', [subject])
        for worker in workers:
            await tx.subject(worker)
        record = {
            'title': args['title'],
            'description': 'Explicit task plan. The runtime starts and stops workers.',
            'requirements': 'Only successful dependencies release a task.',
            'requester': subject,
            'assignee': None,
            'subject': subject,
            'created_at': wire(ctx.now),
            'status': 'open',
            'scheduler_version': 1,
            'workers': workers,
            'graph': task_graph.create(args['tasks'], args['concurrency']),
        }
        parent = await _folder(app, ctx, request, tx, subject, 'request')
        resource = await create_resource(
            app, ctx, request, tx, parent=parent, type=KINDS['request'], name=new_id('plan'),
            body=canonical(record), media_type='application/json', mode=0o600,
        )
        return _result(resource, record)

    async def load(ctx, request, tx, *, mutate=False):
        subject = await _self(app, ctx, request, tx)
        resource, revision, record = await _load(app, ctx, request, tx, 'request')
        require(record.get('scheduler_version') == 1, 'task_plan_required')
        if mutate:
            require(subject in record['workers'], 'task_worker_forbidden')
            await assert_generation(request, resource)
        return subject, resource, revision, record

    @op('communication.task_status', obj({'id': IDENTIFIER}, ('id',)), effect='read')
    async def status(ctx, request, tx):
        _, resource, revision, record = await load(ctx, request, tx)
        return HandlerOutput(data={
            'plan': await _project(app, ctx, request, tx, resource, revision, record),
        })

    async def save(ctx, request, tx, resource, revision, record, graph, *, task=None):
        state = task_graph.summary(graph)
        record = {**record, 'graph': graph, 'updated_at': wire(ctx.now)}
        record['status'] = (
            'fulfilled' if state['succeeded'] == state['total']
            else 'cancelled' if state['done'] else 'claimed' if state['running'] else 'open'
        )
        resource = await revise_resource(
            app, ctx, request, tx, resource, canonical(record), 'application/json',
            relations=revision.relations, preserve_existing_relations=True,
        )
        output = _result(resource, record)
        # No cached instructions, dependency results, or private reference bodies.
        return HandlerOutput(resources=output.resources, data={
            **output.data,
            'task_id': task['id'] if task else None,
            'attempt': task['attempt'] if task else None,
            'summary': state,
        })

    @op(
        'communication.task_claim',
        obj({'id': IDENTIFIER, 'worker': NAME, 'lease_seconds': LEASE}, ('id', 'worker')),
        signature=True,
    )
    async def claim(ctx, request, tx):
        subject, resource, revision, record = await load(ctx, request, tx, mutate=True)
        graph = task_graph.settled(record['graph'], ctx.now.timestamp())
        tasks = {task['id']: task for task in graph['tasks']}
        permitted = set()
        for name in task_graph.summary(graph)['ready']:
            try:
                for dependency in tasks[name]['dependencies']:
                    ref = revision.relations[tasks[dependency]['result_slot']].target
                    await _safe_ref(app, ctx, request, tx, ref.id)
            except Failure as exc:
                if exc.code not in HIDDEN:
                    raise
            else:
                permitted.add(name)
        graph, task = task_graph.claim(
            graph, worker=subject + '#' + request.arguments['worker'], attempt=new_id('attempt'),
            now=ctx.now.timestamp(), lease_seconds=request.arguments.get('lease_seconds', 300),
            permitted=permitted,
        )
        return await save(ctx, request, tx, resource, revision, record, graph, task=task)

    common = {'id': IDENTIFIER, 'task_id': NAME, 'worker': NAME, 'attempt': NAME}

    @op(
        'communication.task_complete',
        obj({**common, 'result': REF, 'error': TEXT}, tuple(common)),
        signature=True,
    )
    async def complete(ctx, request, tx):
        from dataclasses import replace

        subject, resource, revision, record = await load(ctx, request, tx, mutate=True)
        args = request.arguments
        require(('result' in args) != ('error' in args), 'task_result_required')
        relations = list(revision.relations)
        slot = None
        if 'result' in args:
            ref = decode(ResourceRef, args['result'])
            require(ref.revision is not None, 'task_result_revision_required')
            rid = await _safe_ref(app, ctx, request, tx, ref.id)
            ref = ResourceRef(id=rid, revision=ref.revision)
            await tx.revision(ref)
            relation = Relation(type='reference', target=ref)
            if relation not in relations:
                relations.append(relation)
            slot = relations.index(relation)
        graph = task_graph.finish(
            record['graph'], task_id=args['task_id'], worker=subject + '#' + args['worker'],
            attempt=args['attempt'], now=ctx.now.timestamp(), result_slot=slot,
            error=args.get('error'),
        )
        return await save(
            ctx, request, tx, resource, replace(revision, relations=tuple(relations)), record, graph,
            task=next(task for task in graph['tasks'] if task['id'] == args['task_id']),
        )

    @op(
        'communication.task_renew',
        obj({**common, 'lease_seconds': LEASE}, tuple(common)),
        signature=True,
    )
    async def renew(ctx, request, tx):
        subject, resource, revision, record = await load(ctx, request, tx, mutate=True)
        args = request.arguments
        graph = task_graph.renew(
            record['graph'], task_id=args['task_id'], worker=subject + '#' + args['worker'],
            attempt=args['attempt'], now=ctx.now.timestamp(),
            lease_seconds=args.get('lease_seconds', 300),
        )
        return await save(
            ctx, request, tx, resource, revision, record, graph,
            task=next(task for task in graph['tasks'] if task['id'] == args['task_id']),
        )
