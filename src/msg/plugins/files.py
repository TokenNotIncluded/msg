"""File vocabulary over the existing resource use cases and transaction boundary."""

from dataclasses import replace

from msg.core.codec import decode, unb64
from msg.core.errors import require
from msg.core.models import ResourceRef
from msg.plugins.common import check_access, output_for, registration, requirement, revise_resource
from msg.plugins.schemas import BYTES, IDENTIFIER, REF, STRING, obj


def install(app):
    op, finish = registration(app, 'file', ('content', 'discovery', 'batch'))

    def alias(name, target, version=1):
        spec = app.registry.operation(target, version)
        op(
            name,
            app.registry.schema(spec.input_schema),
            effect=spec.effect,
            requirements=spec.requirements,
            signature=spec.require_signature,
            enabled=spec.enabled,
            anonymous_only=spec.anonymous_only,
            requires_rules=('msg.files', 'msg.read-write', 'msg.protocol'),
        )(spec.handler)

    alias('file.create', 'content.file_put')
    copy = app.registry.operation('content.file_put')
    op(
        'file.copy',
        obj({'parent': IDENTIFIER, 'name': STRING, 'source': REF}, ('parent', 'name', 'source')),
        requirements=copy.requirements,
    )(copy.handler)
    alias('file.mkdir', 'content.topic_create')
    alias('file.move', 'content.move')
    alias('file.delete', 'content.archive')
    alias('file.read', 'discovery.get')
    alias('file.list', 'discovery.list')
    alias('file.patch', 'content.text_patch', 3)
    alias('file.batch', 'batch.atomic')

    @op('file.stat', obj({'id': IDENTIFIER}, ('id',)), effect='read')
    async def stat(ctx, request, tx):
        spec = app.registry.operation('discovery.get')
        result = await spec.handler(
            ctx, replace(request, arguments={**request.arguments, 'view': 'meta'}), tx
        )
        resource_type = app.registry.resource_type(result.data['type'], result.data['type_version'])
        return replace(result, data={**result.data, 'container': resource_type.container})

    @op(
        'file.write',
        obj(
            {
                'id': IDENTIFIER,
                'base_revision': IDENTIFIER,
                'data': BYTES,
                'source': REF,
                'media_type': STRING,
            },
            ('id', 'base_revision'),
        ),
        requirements=requirement('id', 'write'),
    )
    async def write(ctx, request, tx):
        from msg.plugins.content import editable_resource

        args = request.arguments
        resource = await editable_resource(app, ctx, request, tx, args['id'])
        require(resource.type == 'file', 'not_editable')
        require(
            resource.revision == args['base_revision'],
            'revision_conflict',
            details={'revision': resource.revision},
        )
        old = await tx.revision(ResourceRef(id=resource.id))
        require(('data' in args) != ('source' in args), 'one_content_source_required')
        if 'source' in args:
            ref = decode(ResourceRef, args['source'])
            await check_access(app, ctx, request, tx, ref.id, 'read')
            body = (await tx.revision(ref)).content
            media = body.media_type
        else:
            body = unb64(args['data'], limit=app.settings.server.limits.max_request_bytes)
            media = args.get('media_type', old.content.media_type)
        require('\r' not in media and '\n' not in media, 'invalid_media_type')
        updated = await revise_resource(
            app, ctx, request, tx, resource, body, media, relations=old.relations, author=old.author
        )
        return output_for(updated)

    finish()
