"""Explicit collaboration records backed by ordinary Resource and Revision facts."""
from msg.core.codec import canonical, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, Relation, ResourceRef, ResourceTypeSpec
from msg.plugins.collaboration import _safe_ref, _self
from msg.plugins.common import (
    assert_generation,
    check_access,
    create_resource,
    new_id,
    resolve,
    revise_resource,
)
from msg.plugins.schemas import IDENTIFIER, STRING, obj

KINDS = {'request': 'collab_request', 'offer': 'collab_offer', 'checkpoint': 'checkpoint', 'proposal': 'collab_proposal'}
REFS = {'type': 'array', 'items': IDENTIFIER, 'maxItems': 16, 'uniqueItems': True}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 4096}


def resource_types(app):
    types = []
    fields = {
        'request': {'title': TEXT, 'description': TEXT, 'requirements': TEXT,
                    'requester': IDENTIFIER, 'assignee': {'type': ['string', 'null']},
                    'due_at': STRING, 'status': {'enum': ['open', 'claimed', 'fulfilled', 'cancelled']}},
        'offer': {'description': TEXT, 'scope': TEXT, 'availability': TEXT,
                  'capability_hint': TEXT, 'status': {'enum': ['active', 'withdrawn']}},
        'proposal': {'author': IDENTIFIER, 'message': TEXT,
                     'status': {'enum': ['open', 'accepted', 'rejected', 'withdrawn', 'superseded']}},
        'checkpoint': {'summary': TEXT, 'resume_hint': TEXT, 'status': {'const': 'active'}},
    }
    required = {'request': ('title', 'description', 'requirements', 'requester', 'assignee'),
                'offer': ('description', 'scope', 'availability'), 'checkpoint': ('summary',), 'proposal': ('author', 'message')}
    for kind, name in KINDS.items():
        ref = ResourceRef(id='schema:collaboration-record:' + kind + ':1')
        app.registry.add_schema(ref, obj({**fields[kind], 'subject': IDENTIFIER,
            'created_at': STRING, 'updated_at': STRING, 'expires_at': STRING},
            (*required[kind], 'subject', 'created_at', 'status')))
        types.append(ResourceTypeSpec(name=name, version=1, container=False,
            content_schema=ref, operations=frozenset(),
            relations=frozenset({'reference', 'state', 'target', 'content'})))
    return tuple(types)


async def _folder(app, ctx, request, tx, subject, kind):
    name = kind + 's'
    row = tx.one('SELECT id FROM resources WHERE parent=? AND name=?', (subject, name))
    if row:
        folder = await tx.resource(row[0])
        require(folder.type == 'topic' and folder.owner == subject and
                folder.state == 'active' and folder.mode == 0o700,
                'collaboration_folder_conflict')
        return folder.id
    folder = await create_resource(app, ctx, request, tx, parent=subject,
        type='topic', name=name, mode=0o700)
    return folder.id


async def _load(app, ctx, request, tx, kind):
    rid = await resolve(tx, request.arguments['id'])
    resource = await tx.resource(rid)
    require(resource.type == KINDS[kind], 'collaboration_not_found')
    await check_access(app, ctx, request, tx, rid, 'read')
    revision = await tx.revision(ResourceRef(id=rid))
    record = loads(await app.contents.read_bytes(revision.content))
    return resource, revision, record


async def _project(app, ctx, request, tx, resource, revision, record):
    result = {**record, 'id': resource.id, 'revision': revision.id,
              'generation': resource.generation, 'resource_refs': []}
    for relation in revision.relations:
        try:
            await _safe_ref(app, ctx, request, tx, relation.target.id)
        except Failure as exc:
            if exc.code in {'permission_denied', 'credential_ceiling', 'certificate_gate',
                            'delegation_scope', 'ancestor_inactive', 'not_found',
                            'collaboration_ref_forbidden'}:
                continue
            raise
        if relation.type in {'target', 'content'}:
            result[relation.type + '_ref'] = wire(relation.target)
        elif relation.type == 'state':
            result['state_ref'] = relation.target.id
        else:
            result['resource_refs'].append(relation.target.id)
    if (record.get('expires_at') and parse_time(record['expires_at']) <= ctx.now
            and record['status'] in {'open', 'claimed', 'active'}):
        result['effective_status'] = 'expired'
    result.setdefault('effective_status', record['status'])
    return result


def _result(resource, record):
    # Replay stores no target refs or authored prose; reads recheck current ACLs.
    return HandlerOutput(resources=(ResourceRef(id=resource.id, revision=resource.revision),),
        data={'id': resource.id, 'generation': resource.generation, 'status': record['status']})


def install(app, op):
    async def create(ctx, request, tx, kind):
        subject = await _self(app, ctx, request, tx)
        args = request.arguments
        for name in ('expires_at', 'due_at'):
            if args.get(name):
                require(parse_time(args[name]) > ctx.now, 'collaboration_expiry_invalid')
        if args.get('due_at') and args.get('expires_at'):
            require(parse_time(args['due_at']) <= parse_time(args['expires_at']),
                    'collaboration_expiry_invalid')
        relations = [Relation(type='reference', target=ResourceRef(
            id=await _safe_ref(app, ctx, request, tx, rid))) for rid in args.get('resource_refs', ())]
        if args.get('state_ref'):
            relations.append(Relation(type='state', target=ResourceRef(
                id=await _safe_ref(app, ctx, request, tx, args['state_ref']))))
        record = {key: value for key, value in args.items() if key not in {'resource_refs', 'state_ref'}}
        record.update(subject=subject, created_at=wire(ctx.now),
                      status='open' if kind == 'request' else 'active')
        if kind == 'request':
            record.update(requester=subject, assignee=None)
        parent = await _folder(app, ctx, request, tx, subject, kind)
        resource = await create_resource(app, ctx, request, tx, parent=parent,
            type=KINDS[kind], name=new_id(kind), body=canonical(record),
            media_type='application/json', relations=relations, mode=0o600)
        return _result(resource, record)

    @op('communication.request_create', obj({'title': TEXT, 'description': TEXT,
        'resource_refs': REFS, 'requirements': TEXT, 'due_at': STRING,
        'expires_at': STRING}, ('title', 'description', 'requirements')), signature=True)
    async def request_create(ctx, request, tx):
        return await create(ctx, request, tx, 'request')

    @op('communication.offer_create', obj({'description': TEXT, 'capability_hint': TEXT,
        'scope': TEXT, 'availability': TEXT, 'resource_refs': REFS,
        'expires_at': STRING}, ('description', 'scope', 'availability')), signature=True)
    async def offer_create(ctx, request, tx):
        return await create(ctx, request, tx, 'offer')

    @op('communication.checkpoint_create', obj({'summary': TEXT, 'resume_hint': TEXT,
        'resource_refs': REFS, 'state_ref': IDENTIFIER, 'expires_at': STRING},
        ('summary', 'resource_refs')), signature=True)
    async def checkpoint_create(ctx, request, tx):
        return await create(ctx, request, tx, 'checkpoint')

    def install_reads(kind):
        @op('communication.' + kind + '_get', obj({'id': IDENTIFIER}, ('id',)), effect='read')
        async def get(ctx, request, tx):
            resource, revision, record = await _load(app, ctx, request, tx, kind)
            return HandlerOutput(data={kind: await _project(app, ctx, request, tx,
                                                          resource, revision, record)})

        @op('communication.' + kind + '_list', obj({
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            'after': IDENTIFIER}), effect='read')
        async def list_records(ctx, request, tx):
            subject = await _self(app, ctx, request, tx)
            limit = request.arguments.get('limit', 50)
            rows = tx.rows('SELECT id FROM resources WHERE owner=? AND type=? '
                           "AND state='active' AND id>? ORDER BY id",
                           (subject, KINDS[kind], request.arguments.get('after', '')))
            items = []
            for (rid,) in rows:
                try:
                    await check_access(app, ctx, request, tx, rid, 'read')
                except Failure as exc:
                    if exc.code in {'permission_denied', 'credential_ceiling', 'certificate_gate',
                                    'delegation_scope', 'ancestor_inactive', 'not_found'}:
                        continue
                    raise
                resource = await tx.resource(rid)
                revision = await tx.revision(ResourceRef(id=rid))
                record = loads(await app.contents.read_bytes(revision.content))
                items.append(await _project(app, ctx, request, tx, resource, revision, record))
                if len(items) > limit:
                    break
            return HandlerOutput(data={'items': items[:limit],
                'next_after': items[limit - 1]['id'] if len(items) > limit else None})

    for kind in KINDS:
        install_reads(kind)

    from msg.plugins.proposals import install as install_proposals
    install_proposals(app, op)

    def install_transition(kind, action):
        @op('communication.' + kind + '_' + action,
            obj({'id': IDENTIFIER}, ('id',)), signature=True)
        async def transition(ctx, request, tx):
            subject = await _self(app, ctx, request, tx)
            resource, revision, record = await _load(app, ctx, request, tx, kind)
            await assert_generation(request, resource)
            require(not record.get('expires_at') or parse_time(record['expires_at']) > ctx.now,
                    'collaboration_expired')
            if action == 'claim':
                require(record['status'] == 'open', 'request_not_open')
                record.update(status='claimed', assignee=subject)
            elif action == 'fulfill':
                require(record['status'] == 'claimed', 'request_not_claimed')
                require(subject == record['assignee'], 'collaboration_actor_forbidden')
                record['status'] = 'fulfilled'
            else:
                require(subject == resource.owner, 'collaboration_actor_forbidden')
                require(record['status'] in ({'open', 'claimed'} if kind == 'request' else {'active'}),
                        'collaboration_inactive')
                record['status'] = 'cancelled' if action == 'cancel' else 'withdrawn'
            record['updated_at'] = wire(ctx.now)
            # The preceding immutable revision retains historical references.
            # The new revision carries only references visible to this actor.
            visible = await _project(app, ctx, request, tx, resource, revision, record)
            refs = set(visible['resource_refs']) | {visible.get('state_ref')}
            relations = tuple(r for r in revision.relations if r.target.id in refs)
            resource = await revise_resource(app, ctx, request, tx, resource,
                canonical(record), 'application/json', relations=relations)
            return _result(resource, record)

    for kind, action in (('request', 'claim'), ('request', 'fulfill'),
                         ('request', 'cancel'), ('offer', 'withdraw')):
        install_transition(kind, action)
