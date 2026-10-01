"""Authorized address discovery shared by every executor transport."""

from msg.core.addressing import parse_address, resource_address
from msg.core.errors import require
from msg.core.models import HandlerOutput, ResourceRef
from msg.plugins.common import check_access, resolve_read
from msg.plugins.schemas import IDENTIFIER, obj


def install(app, op):
    @op(
        'discovery.resolve',
        obj(
            {
                'address': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
                'revision': IDENTIFIER,
            },
            ('address',),
        ),
        effect='read',
    )
    async def resolve_address(ctx, request, tx):
        target, pinned = parse_address(request.arguments['address'], app.settings.service_url)
        explicit = request.arguments.get('revision')
        require(pinned is None or explicit is None or pinned == explicit, 'revision_mismatch')
        rid = await resolve_read(tx, target)
        await check_access(app, ctx, request, tx, rid, 'read')
        resource = await tx.resource(rid)
        require(resource.state != 'purged', 'resource_purged')
        revision = pinned or explicit
        if revision is not None:
            revision = (await tx.revision(ResourceRef(id=rid, revision=revision))).id
        ref = ResourceRef(id=rid, revision=revision)
        return HandlerOutput(
            resources=(ref,),
            data={
                **resource_address(app.settings.service_url, ref),
                'path': await tx.path(rid),
                'current': resource_address(
                    app.settings.service_url, ResourceRef(id=rid, revision=resource.revision)
                ),
            },
        )
