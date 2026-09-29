"""Explicit durable query descriptors, without extending temporary read authority."""

from dataclasses import replace

from msg.core.codec import canonical, decode, digest, loads, parse_time, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput, Principal, ResourceRef, ResourceTypeSpec
from msg.core.read_query import read_query_version
from msg.core.requests import request_for
from msg.core.search_query import search_query_version
from msg.plugins.common import (
    assert_generation,
    check_access,
    create_resource,
    new_id,
    operation_id,
    resolve,
)
from msg.plugins.schemas import IDENTIFIER, STRING, obj

PINNED_REF = obj({'id': IDENTIFIER, 'revision': IDENTIFIER}, ('id', 'revision'))


async def _query_scope(app, ctx, tx, descriptor, principal):
    operation, version = descriptor['operation'], descriptor['contract_version']
    require(
        operation in {'discovery.read_query', 'discovery.lexical_search'}, 'saved_query_invalid'
    )
    spec = app.registry.operation(operation, version)
    require(spec.effect == 'read', 'saved_query_invalid')
    arguments = descriptor['arguments']
    app.registry.validate(spec.input_schema, arguments)
    require('cursor' not in arguments, 'saved_query_invalid')
    scope_id = arguments.get('parent' if operation == 'discovery.read_query' else 'scope')
    require(scope_id is not None, 'saved_query_invalid')
    source_ctx = replace(ctx, principal=principal)
    # This is an authorization query, never a replacement signed operation or
    # execution of a stored result. It checks the original read contract's scope.
    probe = request_for(
        operation,
        arguments,
        app.settings.service_url,
        subject=principal.subject,
        contract_version=version,
    )
    if operation == 'discovery.lexical_search':
        from msg.plugins.discovery import normalize_search_scope

        normalized, _, resources = await normalize_search_scope(
            app, source_ctx, probe, tx, scope_id
        )
        require(all(resource.state == 'active' for resource in resources), 'ancestor_inactive')
        return normalized
    scope_id = await resolve(tx, scope_id)
    scope = await tx.resource(scope_id)
    require(scope.state == 'active', 'ancestor_inactive')
    check = (
        'list' if app.registry.resource_type(scope.type, scope.type_version).container else 'read'
    )
    await check_access(app, source_ctx, probe, tx, scope_id, check)
    return scope_id


async def load_saved_query(app, ctx, request, tx, ref):
    """Internal loader; returned principal must never be serialized to clients."""
    from msg.workers.effects import current_principal

    require(ref.revision is not None, 'saved_query_revision_required')
    resource = await tx.resource(ref.id)
    require(resource.type == 'saved_query' and resource.state == 'active', 'saved_query_not_found')
    require(
        ctx.principal.subject == resource.owner and ctx.principal.actor == resource.owner,
        'permission_denied',
    )
    require(
        resource.mode & 0o400
        and all(item.state == 'active' for item in await tx.ancestors(resource.id)),
        'permission_denied',
    )
    await check_access(app, ctx, request, tx, resource.parent, 'list')
    await app.authorizer.require_base(ctx.principal, operation_id(request), resource.id, tx)
    revision = await tx.revision(ref)
    saved = loads(await app.contents.read_bytes(revision.content, limit=131072))
    require(
        saved.get('subject') == resource.owner
        and set(saved)
        == {
            'subject',
            'created_at',
            'descriptor',
            'descriptor_digest',
            'principal',
            'source_digest',
        },
        'saved_query_invalid',
    )
    app.registry.validate(app.registry.resource_type('saved_query').content_schema, saved)
    descriptor = saved['descriptor']
    require(
        type(descriptor) is dict
        and set(descriptor) == {'operation', 'contract_version', 'arguments'},
        'saved_query_invalid',
    )
    require(digest(descriptor) == saved['descriptor_digest'], 'saved_query_digest_mismatch')
    principal = await current_principal(app, decode(Principal, saved['principal']), tx)
    require(principal.actor == principal.subject == resource.owner, 'permission_denied')
    await app.authorizer.require_base(principal, 'query.save@1', resource.id, tx)
    await _query_scope(app, ctx, tx, descriptor, principal)
    await _query_scope(app, ctx, tx, descriptor, ctx.principal)
    return {**descriptor, 'digest': saved['descriptor_digest'], 'principal': principal}


def install(app, op):
    @op('query.save', obj({'query_ref': STRING}, ('query_ref',)), signature=True)
    async def save(ctx, request, tx):
        from msg.plugins.transfer import query_ref_principal, sealed_read_query

        require(not request.return_fields, 'projection_unavailable')
        subject = ctx.principal.subject
        require(subject is not None and subject == ctx.principal.actor, 'permission_denied')
        await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
        token = app.cursors.inspect(request.arguments['query_ref'])
        require(token.get('kind') == 'query-ref', 'invalid_query_ref')
        query, position = token['query'], token['position']
        require(query.get('service') == app.settings.service_url, 'wrong_service')
        require(
            position.get('principal') == query_ref_principal(ctx.principal),
            'query_ref_principal_mismatch',
        )
        require(ctx.now < parse_time(position['expires_at']), 'query_ref_expired')
        transfer = await tx.transfer(query['transfer_id'])
        ref, revision, kind, args = await sealed_read_query(app, ctx, request, tx, transfer)
        require(
            query.get('output') == wire(ref)
            and query.get('digest') == revision.content.digest
            and query.get('query_kind') == kind,
            'query_ref_digest_mismatch',
        )
        operation = 'discovery.read_query' if kind == 'read' else 'discovery.lexical_search'
        version = read_query_version(args) if kind == 'read' else search_query_version(args)
        arguments = dict(args)
        for field in ('parent', 'author', 'owner', 'relation_to', 'relation_from'):
            if field in arguments:
                arguments[field] = await resolve(tx, arguments[field])
        descriptor = {'operation': operation, 'contract_version': version, 'arguments': arguments}
        normalized_scope = await _query_scope(app, ctx, tx, descriptor, ctx.principal)
        if kind == 'search':
            arguments['scope'] = normalized_scope
        saved = {
            'subject': subject,
            'created_at': wire(ctx.now),
            'descriptor': descriptor,
            'descriptor_digest': digest(descriptor),
            'principal': wire(ctx.principal),
            'source_digest': revision.content.digest,
        }
        resource = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent=subject,
            type='saved_query',
            name=new_id('query'),
            mode=0o600,
            body=canonical(saved),
            media_type='application/json',
        )
        output = ResourceRef(id=resource.id, revision=resource.revision)
        return HandlerOutput(
            resources=(output,),
            data={'saved_ref': wire(output), 'descriptor_digest': saved['descriptor_digest']},
        )

    @op('query.saved_get', obj({'ref': PINNED_REF}, ('ref',)), effect='read')
    async def get(ctx, request, tx):
        descriptor = await load_saved_query(
            app, ctx, request, tx, decode(ResourceRef, request.arguments['ref'])
        )
        return HandlerOutput(
            data={key: value for key, value in descriptor.items() if key != 'principal'}
        )

    @op('query.saved_archive', obj({'ref': PINNED_REF}, ('ref',)), signature=True)
    async def archive(ctx, request, tx):
        # Owner may revoke this saved source even if its creation credential or
        # referenced scope is no longer usable. This cannot restore authority.
        ref = decode(ResourceRef, request.arguments['ref'])
        resource = await tx.resource(ref.id)
        require(
            resource.type == 'saved_query'
            and resource.owner == ctx.principal.subject == ctx.principal.actor,
            'permission_denied',
        )
        await app.authorizer.require_base(ctx.principal, operation_id(request), resource.id, tx)
        require(resource.revision == ref.revision, 'revision_conflict')
        await assert_generation(request, resource)
        updated = replace(
            resource,
            state='archived',
            generation=resource.generation + 1,
            modified_at=ctx.now,
            modified_by=ctx.principal.actor,
        )
        await tx.replace(updated, resource.generation)
        return HandlerOutput(
            data={'id': resource.id, 'state': 'archived', 'generation': updated.generation}
        )

    schema_ref = ResourceRef(id='schema:saved-query-record:1')
    app.registry.add_schema(
        schema_ref,
        obj(
            {
                'subject': IDENTIFIER,
                'created_at': STRING,
                'source_digest': STRING,
                'descriptor_digest': STRING,
                'principal': {'type': 'object'},
                'descriptor': obj(
                    {
                        'operation': {'enum': ['discovery.read_query', 'discovery.lexical_search']},
                        'contract_version': {'type': 'integer', 'minimum': 1},
                        'arguments': {'type': 'object'},
                    },
                    ('operation', 'contract_version', 'arguments'),
                ),
            },
            (
                'subject',
                'created_at',
                'source_digest',
                'descriptor_digest',
                'principal',
                'descriptor',
            ),
        ),
    )
    return ResourceTypeSpec(
        name='saved_query',
        version=1,
        container=False,
        content_schema=schema_ref,
        operations=frozenset(),
        relations=frozenset(),
    )
