"""URL and compression boundaries; common wire contracts live in core.packet."""
from __future__ import annotations
import zlib
from msg.core.codec import unb64
from msg.core.errors import require, Failure
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION
from msg.core.models import OperationRequest, TokenProof
from msg.core.packet import (
    REQUEST_SCHEMA, RESULT_SCHEMA, decode_packet, decode_result, safe_error_code,
)
from msg.transports.url_safety import contains_secret_fields


def require_url_safe_packet(packet: OperationRequest) -> None:
    """Reject credentials and recovery claims before a GET URL is dispatched.

    A signature is a bounded request proof; TokenProof and issuance claims are
    reusable secrets. This guards both clients constructing URL packets and the
    server receiving hand-built packets, including gzip envelopes.
    """
    require(not isinstance(packet.proof, TokenProof), 'secure_channel_required')
    require(packet.operation not in SECRET_DELIVERY_MIN_VERSION, 'secure_channel_required')

    require(not contains_secret_fields(packet.arguments), 'secure_channel_required')


def gunzip(raw,limit):
    inflater=zlib.decompressobj(wbits=31)
    try:
        result=inflater.decompress(raw,limit+1)
        require(len(result)<=limit and not inflater.unconsumed_tail,'request_too_large')
        result+=inflater.flush(limit+1-len(result))
        require(len(result)<=limit and inflater.eof and not inflater.unused_data,'invalid_gzip')
        return result
    except zlib.error as exc:
        raise Failure('invalid_gzip') from exc


def path_packet(encoded,encoding,limit):
    raw=unb64(encoded,limit=limit)
    require(encoding in {'j','gz'},'unknown_encoding')
    return decode_packet(gunzip(raw,limit) if encoding=='gz' else raw,limit)
