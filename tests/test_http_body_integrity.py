"""Reject ambiguous framing before reading; bound wire and decoded bodies."""

import gzip

import pytest
from starlette.requests import Request

from msg.core.errors import Failure
from msg.transports.http_common import body_bytes


def streaming_request(headers=(), chunks=(b'abc',)):
    consumed = []
    messages = iter(chunks)

    async def receive():
        data = next(messages, None)
        consumed.append(data)
        return {'type': 'http.request', 'body': data or b'', 'more_body': data is not None}

    request = Request({'type': 'http', 'headers': list(headers)}, receive)
    return request, consumed


@pytest.mark.asyncio
@pytest.mark.parametrize('declared', [b'0', b'2', b'4'])
async def test_declared_and_received_content_length_must_match(declared):
    request, _ = streaming_request([(b'content-length', declared)])
    with pytest.raises(Failure, match='invalid_request'):
        await body_bytes(request, 64)


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [
    [(b'content-length', b'3'), (b'content-length', b'3')],
    [(b'content-length', b'3'), (b'content-length', b'4')],
    [(b'content-length', b'3, 3')],
    [(b'content-length', b'+3')],
    [(b'content-length', b'-3')],
    [(b'content-length', b'')],
    [(b'content-length', b'3'), (b'transfer-encoding', b'chunked')],
    [(b'content-encoding', b'gzip'), (b'content-encoding', b'identity')],
])
async def test_ambiguous_framing_is_rejected_without_consuming_body(headers):
    request, consumed = streaming_request(headers)
    with pytest.raises(Failure, match='invalid_request'):
        await body_bytes(request, 64)
    assert consumed == []


@pytest.mark.asyncio
@pytest.mark.parametrize('declared', [b'65', b'9' * 5000], ids=['over-limit', 'huge-decimal'])
async def test_large_decimal_lengths_cannot_escape_as_integer_conversion_errors(declared):
    request, consumed = streaming_request([(b'content-length', declared)])
    with pytest.raises(Failure, match='request_too_large'):
        await body_bytes(request, 64)
    assert consumed == []


@pytest.mark.asyncio
async def test_unknown_encoding_is_rejected_before_streaming():
    request, consumed = streaming_request([(b'content-encoding', b'br')])
    with pytest.raises(Failure, match='unknown_encoding'):
        await body_bytes(request, 64)
    assert consumed == []


@pytest.mark.asyncio
@pytest.mark.parametrize('headers,chunks,expected', [
    ([], (b'a', b'bc'), b'abc'),
    ([(b'content-length', b'0003')], (b'abc',), b'abc'),
    ([(b'content-length', b'0')], (), b''),
    ([(b'content-encoding', b'IDENTITY')], (b'abc',), b'abc'),
    ([(b'transfer-encoding', b'chunked')], (b'a', b'bc'), b'abc'),
])
async def test_valid_streams_keep_their_bytes(headers, chunks, expected):
    request, _ = streaming_request(headers, chunks)
    assert await body_bytes(request, 64) == expected


@pytest.mark.asyncio
async def test_gzip_content_length_measures_wire_bytes_not_decoded_bytes():
    raw = gzip.compress(b'hello' * 8)
    request, _ = streaming_request(
        [(b'content-length', str(len(raw)).encode()), (b'content-encoding', b'GZip')],
        (raw[:5], raw[5:]),
    )
    assert await body_bytes(request, 64) == b'hello' * 8


@pytest.mark.asyncio
@pytest.mark.parametrize('raw,code', [
    (gzip.compress(b'x' * 256), 'request_too_large'),
    (gzip.compress(b'abc')[:-1], 'invalid_gzip'),
    (gzip.compress(b'abc') + b'trailing', 'invalid_gzip'),
    (gzip.compress(b'abc') + gzip.compress(b'def'), 'invalid_gzip'),
])
async def test_gzip_rejects_expansion_truncation_and_extra_members(raw, code):
    request, _ = streaming_request([(b'content-encoding', b'gzip')], (raw,))
    with pytest.raises(Failure, match=code):
        await body_bytes(request, 64)


@pytest.mark.asyncio
async def test_stream_overflow_stops_before_consuming_further_chunks():
    request, consumed = streaming_request(chunks=(b'x' * 65, b'not read'))
    with pytest.raises(Failure, match='request_too_large'):
        await body_bytes(request, 64)
    assert len(consumed) == 1