"""A private, read-only projection of the caller's persisted OperationResults.

Receipts are the signed results the executor already committed; this view adds
no receipt state, table or writable resource. Stored bytes are never rewritten.
"""
from msg.core.codec import decode, digest, loads
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, ResourceRef
from msg.plugins.common import operation_id
from msg.plugins.discovery import next_link, visible
from msg.plugins.schemas import STRING, obj

REQUEST_ID = {'type': 'string', 'minLength': 1, 'maxLength': 128}


async def _subject(app, ctx, request, tx):
    subject = ctx.principal.subject
    require(subject is not None, 'authentication_required')
    await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
    return subject


async def _project(app, ctx, request, tx, raw):
    saved = loads(raw)
    resources = []
    # A receipt names resources as they were at commit time; access may have
    # been revoked since, so only currently readable references are disclosed.
    for value in saved.get('resources') or ():
        ref = decode(ResourceRef, value)
        try:
            shown = await visible(app, ctx, request, tx, ref.id)
        except Failure as exc:
            if exc.code != 'not_found':
                raise
            shown = False
        if shown:
            resources.append(value)
    # data/output may carry delivered credentials or other presentation-only
    # material, so they are deliberately not part of this projection.
    item = {'request_id': saved['request_id'], 'operation': saved['operation'],
            'status': saved['status'], 'committed_at': saved.get('committed_at'),
            'resources': resources, 'signed': saved.get('receipt') is not None}
    error = saved.get('error')
    if error:
        item['error_code'] = error['code']
    return item


def install(app, op):
    @op('communication.receipt_list', obj({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                                           'cursor': STRING}), effect='read')
    async def receipt_list(ctx, request, tx):
        subject = await _subject(app, ctx, request, tx)
        binding = digest({'subject': subject})
        cursor = request.arguments.get('cursor')
        after = app.cursors.decode(cursor, request.operation, binding) if cursor else ''
        require(type(after) is str, 'invalid_cursor')
        limit = request.arguments.get('limit', 50)
        rows = tx.rows('SELECT request_id,body FROM results WHERE subject=? AND request_id>? '
                       'ORDER BY request_id LIMIT ?', (subject, after, limit + 1))
        items = [await _project(app, ctx, request, tx, raw) for _, raw in rows[:limit]]
        data = {'items': items}
        if len(rows) > limit:
            token = app.cursors.encode(request.operation, binding, rows[limit - 1][0])
            data.update(cursor=token,
                        next=next_link(app, request.operation, {**request.arguments, 'cursor': token}),
                        next_requires_auth=True)
        return HandlerOutput(data=data)

    @op('communication.receipt_get', obj({'request_id': REQUEST_ID}, ('request_id',)), effect='read')
    async def receipt_get(ctx, request, tx):
        subject = await _subject(app, ctx, request, tx)
        row = tx.one('SELECT body FROM results WHERE subject=? AND request_id=?',
                     (subject, request.arguments['request_id']))
        require(row is not None, 'not_found')
        return HandlerOutput(data={'receipt': await _project(app, ctx, request, tx, row[0])})
