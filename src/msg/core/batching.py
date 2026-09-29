"""The one child-envelope validator for atomic and independent batches."""

from msg.core.codec import canonical
from msg.core.errors import require
from msg.core.packet import decode_packet
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION

# Credential delivery has response-only secrets; it requires its own call.
NO_BATCH = frozenset(SECRET_DELIVERY_MIN_VERSION) | {
    'identity.register',
    'identity.upgrade',
    'file.batch',
}


def packets(registry, request, subject, max_bytes=None):
    max_bytes = max_bytes or len(canonical(request))
    values = []
    for value in request.arguments['requests']:
        packet = decode_packet(value, max_bytes)
        spec = registry.operation(packet.operation, packet.contract_version)
        require(
            spec.effect in {'read', 'transaction'}
            and not packet.operation.startswith('batch.')
            and packet.operation not in NO_BATCH
            and 'network' in spec.entries,
            'operation_not_batchable',
        )
        require(packet.subject == subject, 'batch_subject_mismatch')
        values.append(packet)
    require(len({p.request_id for p in values}) == len(values), 'duplicate_batch_request_id')
    require(request.request_id not in {p.request_id for p in values}, 'recursive_request_id')
    return values
