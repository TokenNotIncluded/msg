"""One bounded child-admission policy for both named batch primitives."""
from __future__ import annotations

from .codec import canonical
from .errors import require
from .execution_ports import PacketDecoder
from .requests import SECRET_DELIVERY_MIN_VERSION

# Response-only credentials require their own non-batched delivery call.
NO_BATCH = frozenset(SECRET_DELIVERY_MIN_VERSION) | {'identity.register', 'identity.upgrade'}


class BatchPolicy:
    def __init__(self, registry, decoder: PacketDecoder | None, max_request_bytes: int):
        self.registry = registry
        self.decoder = decoder
        self.max_request_bytes = max_request_bytes

    def packets(self, request, subject, *, bounded=True):
        require(self.decoder is not None, 'batch_decoder_not_configured')
        # Independent batches historically use the validated parent envelope size.
        max_bytes = self.max_request_bytes if bounded else len(canonical(request))
        values = []
        for value in request.arguments['requests']:
            packet = self.decoder(value, max_bytes)
            spec = self.registry.operation(packet.operation, packet.contract_version)
            require(spec.effect in {'read', 'transaction'} and not packet.operation.startswith('batch.')
                    and packet.operation not in NO_BATCH and 'network' in spec.entries,
                    'operation_not_batchable')
            require(packet.subject == subject, 'batch_subject_mismatch')
            values.append(packet)
        require(len({p.request_id for p in values}) == len(values), 'duplicate_batch_request_id')
        require(request.request_id not in {p.request_id for p in values}, 'recursive_request_id')
        return values
