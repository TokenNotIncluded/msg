"""SVG bytes are a separate browser image response, never board/API context."""

import re
from hashlib import sha256

from starlette.responses import Response

from msg.core.board_art import board_svg
from msg.core.codec import decode, wire
from msg.core.errors import require
from msg.core.models import BlobRef
from msg.core.profile_art import MAX_SVG_BYTES, safe_svg, still_svg
from msg.core.requests import request_for
from msg.transports.http_common import BASE_HEADERS

ART_PATH = re.compile(r'/_board/([a-zA-Z0-9_]+)/header\.svg')


async def board_art_response(service, request, execute):
    match = ART_PATH.fullmatch(request.url.path)
    require(match is not None, 'not_found')
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    pairs = request.query_params.multi_items()
    require(not pairs or pairs == [('still', '1')], 'unknown_query_parameter')
    require(not request.headers.get('x-msg-request'), 'representation_mismatch')
    topic = match.group(1)
    account = await execute(
        request_for(
            'discovery.get',
            {'id': topic, 'fields': ['id', 'type', 'name', 'presentation']},
            service.settings.service_url,
            source='manual',
        )
    )
    require(not account.error, account.error.code if account.error else 'not_found')
    require(account.data.get('type') == 'topic' and 'presentation' in account.data, 'not_found')
    svg = None
    file_id = account.data.get('presentation', {}).get('header', {}).get('file')
    if file_id:
        custom = await execute(
            request_for(
                'discovery.raw', {'id': file_id}, service.settings.service_url, source='manual'
            )
        )
        if not custom.error:
            blob = decode(BlobRef, wire(custom.data['content']))
            if blob.media_type == 'image/svg+xml' and blob.size <= MAX_SVG_BYTES:
                raw = b''.join([chunk async for chunk in service.contents.read(blob)])
                svg = safe_svg(raw)
    source = 'custom' if svg else 'generated'
    svg = svg or board_svg(account.data['id'], account.data['name'])
    if pairs:
        svg = still_svg(svg)
    body = svg.encode()
    etag = '"' + sha256(body).hexdigest() + '"'
    headers = {
        **BASE_HEADERS,
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        'Cache-Control': 'private, no-cache',
        'ETag': etag,
        'X-Msg-Artwork-Source': source,
        'Vary': 'Cookie',
    }
    # Access is checked on every request before returning even a cache hit.
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    headers['Content-Length'] = str(len(body))
    return Response(
        b'' if request.method == 'HEAD' else body, media_type='image/svg+xml', headers=headers
    )
