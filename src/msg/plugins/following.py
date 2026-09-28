"""A private, bounded read projection of existing watch rows, never a new feed."""
from datetime import timedelta

from msg.core.codec import wire
from msg.core.errors import require
from msg.core.models import HandlerOutput
from msg.core.read_query import ReadBudget
from msg.plugins.common import operation_id
from msg.plugins.discovery import short_subject_path, visible
from msg.plugins.schemas import obj


FOLLOWING_SCHEMA = obj({
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
    'cursor': {'type': 'string', 'minLength': 1, 'maxLength': 8192},
})


def install(app, op):
    @op('communication.following', FOLLOWING_SCHEMA, effect='read')
    async def following(ctx, request, tx):
        subject = ctx.principal.subject
        require(subject is not None, 'authentication_required')
        await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
        budget = ReadBudget(ctx.deadline_monotonic, app.settings.server.limits.max_response_bytes)
        principal = {'actor': ctx.principal.actor, 'subject': subject,
                     'credential_id': ctx.principal.credential_id}
        arguments = dict(request.arguments)
        after, snapshot = '', ctx.now
        if 'cursor' in arguments:
            require(set(arguments) == {'cursor'}, 'cursor_query_mismatch')
            token = arguments['cursor']
            query, _ = app.cursors.inspect_page(token, ctx.now)
            require(query.get('operation') == request.operation, 'cursor_query_mismatch')
            arguments = query['arguments']
            app.registry.validate(app.registry.operation(request.operation).input_schema, arguments)
            require(set(arguments) == {'limit'}, 'cursor_query_mismatch')
            after, snapshot = app.cursors.decode_page(token, request.operation, arguments,
                                                     principal, ctx.now)
        else:
            arguments = {'limit': arguments.get('limit', 20)}
        limit = arguments['limit']
        items = []
        more = False
        # Watches have no historical membership snapshot. The cursor bounds
        # Resource creation and stable ID order; removals/revocation always win.
        # Keyset scanning remains bounded even if most rows are now invisible.
        while True:
            budget.check()
            rows = list(tx.execute(
                'SELECT r.id FROM watches AS w JOIN resources AS r ON r.id=w.resource '
                "WHERE w.subject=? AND r.state='active' AND r.created_at<=? AND r.id>? "
                'ORDER BY r.id LIMIT 128', (subject, wire(snapshot), after)))
            for (rid,) in rows:
                budget.scan()
                after = rid
                if not await visible(app, ctx, request, tx, rid):
                    continue
                if len(items) == limit:
                    more = True
                    break
                resource = await tx.resource(rid)
                budget.node(5)
                items.append({'id': rid, 'type': resource.type, 'name': resource.name,
                              'path': short_subject_path(await tx.path(rid)),
                              'revision': resource.revision})
            if more or len(rows) < 128:
                break
        # Never encode a hidden/scanned resource ID or a private count in the
        # cursor. A cursor authenticates position, not access to any resource.
        token = (app.cursors.encode_page(request.operation, arguments, items[-1]['id'],
                                        snapshot, principal, ctx.now + timedelta(minutes=15))
                 if more else None)
        data = {'items': items, 'pageInfo': {'hasNextPage': more, 'endCursor': token}}
        if token:
            data.update(cursor=token, next='/_r/c/' + token, next_requires_auth=True)
        budget.output(data)
        return HandlerOutput(data=data)
