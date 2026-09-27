"""Bounded batching is a protocol primitive, not an executable workflow."""
from __future__ import annotations
from msg.core.codec import wire,canonical
from msg.core.errors import Failure,require
from msg.core.executor import result_wire
from msg.core.models import HandlerOutput
from msg.plugins.common import registration,operation_id
from msg.plugins.schemas import obj
from msg.transports.packet import REQUEST_SCHEMA,decode_packet

# Credential delivery has response-only secrets; it requires its own call.
NO_BATCH=frozenset({'identity.register','identity.temporary','identity.token_create','identity.token_rotate',
                    'identity.custodial_create','identity.token_recover','identity.upgrade'})


def packets(registry,request,subject,max_bytes=None):
    max_bytes=max_bytes or len(canonical(request))
    values=[]
    for value in request.arguments['requests']:
        packet=decode_packet(value,max_bytes)
        spec=registry.operation(packet.operation,packet.contract_version)
        require(spec.effect in {'read','transaction'} and not packet.operation.startswith('batch.')
                and packet.operation not in NO_BATCH and 'network' in spec.entries,'operation_not_batchable')
        require(packet.subject==subject,'batch_subject_mismatch')
        values.append(packet)
    require(len({p.request_id for p in values})==len(values),'duplicate_batch_request_id')
    require(request.request_id not in {p.request_id for p in values},'recursive_request_id')
    return values


def install(app):
    op,finish=registration(app,'batch',('identity',))
    schema=obj({'requests':{'type':'array','minItems':1,'maxItems':32,'items':REQUEST_SCHEMA}},('requests',))

    @op('batch.atomic',schema)
    async def atomic(ctx,request,tx):
        await app.authorizer._ceiling(ctx.principal,operation_id(request),ctx.principal.subject,tx)
        children=packets(app.registry,request,ctx.principal.subject,app.settings.server.limits.max_request_bytes)
        results=[];resources=[]
        for index,packet in enumerate(children):
            result=await app.executor.execute(packet,entry=ctx.entry)
            if result.status=='error':
                raise Failure('batch_aborted',f'requests.{index}',details={'index':index,'error_code':result.error.code})
            results.append(result_wire(result));resources.extend(result.resources)
        return HandlerOutput(resources=tuple(dict.fromkeys(resources)),data={'results':results,'atomic':True})

    @op('batch.independent',schema,effect='external')
    async def independent(ctx,request,tx):
        # This function is never executed inside a shared SQL transaction: the
        # executor owns the per-item commit boundary for this named primitive.
        raise Failure('independent_batch_requires_coordinator')

    finish()
