"""Small HTTP response/body primitives shared without importing the router."""

from __future__ import annotations

from starlette.responses import Response

from msg.core.codec import canonical
from msg.core.errors import require
from msg.transports.packet import gunzip

BASE_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': "default-src 'none'; sandbox",
    'Cache-Control': 'no-store',
}


async def body_bytes(request, limit):
    """Validate framing before consumption and bound compressed and decoded bytes."""
    lengths = request.headers.getlist('content-length')
    encodings = request.headers.getlist('content-encoding')
    require(len(lengths) <= 1 and len(encodings) <= 1, 'invalid_request')
    require(not (lengths and 'transfer-encoding' in request.headers), 'invalid_request')
    encoding = encodings[0].strip(' \t').lower() if encodings else 'identity'
    require(encoding in {'identity', 'gzip'}, 'unknown_encoding')
    expected = None
    if lengths:
        length = lengths[0].strip(' \t')
        require(length.isascii() and length.isdecimal(), 'invalid_request')
        # Compare bounded strings before int(): huge headers must not raise ValueError.
        digits = length.lstrip('0') or '0'
        maximum = str(limit)
        require(
            len(digits) < len(maximum)
            or (len(digits) == len(maximum) and digits <= maximum),
            'request_too_large',
        )
        expected = int(digits)
    body = bytearray()
    async for data in request.stream():
        size = len(body) + len(data)
        require(size <= limit, 'request_too_large')
        require(expected is None or size <= expected, 'invalid_request')
        body.extend(data)
    require(expected is None or len(body) == expected, 'invalid_request')
    return gunzip(bytes(body), limit) if encoding == 'gzip' else bytes(body)


def json_response(value, status=200, headers=None):
    return Response(
        canonical(value),
        status_code=status,
        media_type='application/json',
        headers={**BASE_HEADERS, **(headers or {})},
    )


def error_status(code):
    if code == 'handle_rename_cooldown':
        return 429
    if code == 'handle_unavailable':
        return 409
    if code == 'range_not_satisfiable':
        return 416
    if code in {
        'not_found',
        'resource_purged',
        'revision_not_found',
        'csr_not_found',
        'certificate_not_found',
        'listing_not_found',
        'package_not_found',
        'bounty_not_found',
        'order_not_found',
        'delivery_not_found',
        'offer_not_found',
    }:
        return 404
    if code in {
        'authentication_required',
        'invalid_token',
        'invalid_signature',
        'credential_revoked',
        'credential_expired',
        'request_expired',
    }:
        return 401
    if code in {
        'permission_denied',
        'local_only',
        'credential_ceiling',
        'certificate_gate',
        'tool_certificate_required',
        'forbidden_origin',
        'forbidden_host',
        'passive_client_forbidden',
        'query_ref_principal_mismatch',
        'cursor_principal_mismatch',
    }:
        return 403
    if code in {
        'generation_conflict',
        'revision_conflict',
        'idempotency_conflict',
        'chunk_conflict',
        'constraint_conflict',
    }:
        return 409
    if code in {
        'request_too_large',
        'path_too_large',
        'response_too_large',
        'use_transfer',
        'part_too_large',
    }:
        return 413
    if code in {'method_not_allowed', 'effect_mismatch'}:
        return 405
    if code == 'secure_channel_required':
        return 400
    if code in {
        'server_busy',
        'issuer_not_ready',
        'dependency_unavailable',
        'service_restart_required',
        'writes_paused',
    }:
        return 503
    if code == 'storage_capacity_exceeded':
        return 507
    if code == 'internal_error':
        return 500
    return 400
