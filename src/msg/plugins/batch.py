"""Bounded batching is a protocol primitive, not an executable workflow."""

from __future__ import annotations

from msg.core.batching import packets
from msg.core.errors import Failure
from msg.core.models import HandlerOutput
from msg.core.packet import REQUEST_SCHEMA, result_wire
from msg.plugins.common import operation_id, registration
from msg.plugins.schemas import obj


def install(app):
    op, finish = registration(app, 'batch', ('identity',))
    schema = obj(
        {'requests': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': REQUEST_SCHEMA}},
        ('requests',),
    )

    @op('batch.atomic', schema)
    async def atomic(ctx, request, tx):
        await app.authorizer._ceiling(
            ctx.principal, operation_id(request), ctx.principal.subject, tx
        )
        children = packets(
            app.registry,
            request,
            ctx.principal.subject,
            app.settings.server.limits.max_request_bytes,
        )
        results = []
        resources = []
        for index, packet in enumerate(children):
            result = await app.executor.execute(packet, entry=ctx.entry)
            if result.status == 'error':
                raise Failure(
                    'batch_aborted',
                    f'requests.{index}',
                    details={'index': index, 'error_code': result.error.code},
                )
            results.append(result_wire(result))
            resources.extend(result.resources)
        return HandlerOutput(
            resources=tuple(dict.fromkeys(resources)), data={'results': results, 'atomic': True}
        )

    @op('batch.independent', schema, effect='external')
    async def independent(ctx, request, tx):
        # This function is never executed inside a shared SQL transaction: the
        # executor owns the per-item commit boundary for this named primitive.
        raise Failure('independent_batch_requires_coordinator')

    finish()
