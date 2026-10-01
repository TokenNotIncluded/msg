"""SVG bytes are a separate browser image response, never profile/API context."""

import re
from hashlib import sha256

from starlette.responses import Response

from msg.core.codec import decode, wire
from msg.core.errors import require
from msg.core.models import BlobRef
from msg.core.profile_art import MAX_SVG_BYTES, generate_svg, safe_svg, still_svg
from msg.core.requests import request_for
from msg.transports.http_common import BASE_HEADERS

ART_PATH = re.compile(r'/(@[^/]+)/art/(avatar|background|footer)\.svg')
ART_FILES = {'avatar': 'AVATAR.svg', 'background': 'BACKGROUND.svg', 'footer': 'FOOTER.svg'}


async def profile_art_response(service, request, execute):
    match = ART_PATH.fullmatch(request.url.path)
    require(match is not None, 'not_found')
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    pairs = request.query_params.multi_items()
    require(not pairs or pairs == [('still', '1')], 'unknown_query_parameter')
    require(not request.headers.get('x-msg-request'), 'representation_mismatch')
    handle, kind = match.groups()
    account = await execute(
        request_for(
            'discovery.get',
            {'id': '/' + handle, 'fields': ['id', 'type']},
            service.settings.service_url,
            source='manual',
        )
    )
    require(not account.error, account.error.code if account.error else 'not_found')
    require(account.data.get('type') == 'user', 'not_found')
    svg = None
    custom = await execute(
        request_for(
            'discovery.raw',
            {'id': '/' + handle + '/' + ART_FILES[kind]},
            service.settings.service_url,
            source='manual',
        )
    )
    if not custom.error:
        blob = decode(BlobRef, wire(custom.data['content']))
        if blob.media_type == 'image/svg+xml' and blob.size <= MAX_SVG_BYTES:
            raw = b''.join([chunk async for chunk in service.contents.read(blob)])
            svg = safe_svg(raw)
    source = 'custom' if svg else 'generated'
    svg = svg or generate_svg(account.data['id'], kind)
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
