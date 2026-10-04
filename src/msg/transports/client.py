"""Client-side envelopes; transport selection never changes business semantics."""

from __future__ import annotations

import gzip
from urllib.parse import urlsplit

import httpx

from msg.core.codec import b64, canonical, decode, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import TransportLimits
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION
from msg.service_origin import service_origin
from msg.transports.packet import decode_result, require_url_safe_packet, safe_error_code
from msg.transports.url_safety import require_safe_relative_url


class HTTPTransport:
    name = 'http'

    def __init__(
        self, server, *, endpoint=None, http=None, max_response_bytes=1048576, max_path_bytes=8192
    ):
        server = service_origin(server)
        self.server = server
        self.endpoint = service_origin(endpoint) if endpoint is not None else server
        require(
            urlsplit(self.endpoint).scheme == urlsplit(server).scheme,
            'endpoint_scheme_mismatch',
        )
        self.http = http or httpx.AsyncClient(
            base_url=self.endpoint, timeout=30, follow_redirects=False
        )
        self._owns_http = http is None
        self.max_response_bytes = max_response_bytes
        self.max_path_bytes = max_path_bytes
        self._description = None
        self.calls = 0
        self.bytes_received = 0
        self.bytes_sent = 0

    async def _json(self, method, path, *, body=None, headers=None, maximum=None, read_only=False):
        require_safe_relative_url(path, maximum=self.max_path_bytes)
        limit = maximum or self.max_response_bytes
        raw = canonical(body) if body is not None else None
        headers = dict(headers or {})
        if raw is not None:
            headers.setdefault('Content-Type', 'application/json')
        self.calls += 1
        self.bytes_sent += len(raw or b'') + len(path.encode())
        # Never follow a credential-bearing operation to another origin.
        async with self.http.stream(
            method, self.endpoint + path, content=raw, headers=headers, follow_redirects=False
        ) as response:
            require(not response.is_redirect, 'redirect_not_allowed')
            data = bytearray()
            async for piece in response.aiter_bytes():
                require(len(data) + len(piece) <= limit, 'response_too_large')
                data.extend(piece)
            self.bytes_received += len(data)
            if response.status_code == 202 and not data:
                return None
            try:
                value = loads(bytes(data))
            except Failure as exc:
                media_type = (
                    response.headers.get('content-type', '').partition(';')[0].strip().lower()
                )
                # A restarting gateway can return HTML or an empty body. Only
                # declared reads recover; strict JSON rejections stay terminal.
                if (
                    read_only
                    and response.status_code in {502, 503, 504}
                    and exc.code == 'invalid_json'
                    and 'json' not in media_type
                ):
                    raise Failure('transport_error', retryable=True) from None
                raise Failure('invalid_server_response') from None
            require(isinstance(value, dict), 'invalid_server_response')
            if response.status_code >= 400 and 'status' not in value:
                error = value.get('error')
                code = (
                    error
                    if isinstance(error, str)
                    else (error.get('code') if isinstance(error, dict) else None)
                )
                raise Failure(
                    safe_error_code(code, 'transport_error'),
                    retryable=response.status_code in {502, 503, 504},
                )
            return value

    async def description(self):
        if self._description is None:
            value = await self._json('GET', '/_transports', read_only=True)
            require(
                value.get('version') == 1 and value.get('target_service') == self.server,
                'service_mismatch',
            )
            require(isinstance(value.get('operations'), dict), 'invalid_transport_description')
            self._description = value
        return self._description

    async def discover(self):
        value = await self.description()
        limits = dict(value['limits'])
        limits['max_response_bytes'] = min(limits['max_response_bytes'], self.max_response_bytes)
        limits['max_path_bytes'] = min(limits['max_path_bytes'], self.max_path_bytes)
        return decode(TransportLimits, limits)

    async def _effect(self, name):
        description = await self.description()
        effect = description['operations'].get(name)
        require(effect in {'read', 'transaction', 'external'}, 'unknown_operation')
        return effect

    def _secure_delivery(self, request):
        if request.operation in SECRET_DELIVERY_MIN_VERSION:
            parsed = urlsplit(self.endpoint)
            require(
                parsed.scheme == 'https'
                or parsed.hostname in {'testserver', 'localhost', '127.0.0.1', '::1'},
                'secure_channel_required',
            )

    async def call(self, request):
        self._secure_delivery(request)
        effect = await self._effect(request.operation)
        path = '/-/p/' + request.operation
        # Signed reads use POST to a query endpoint: read/write is the operation's
        # declared effect, not inferred from whether HTTP has a request body.
        value = await self._json('POST', path, body=wire(request), read_only=effect == 'read')
        return decode_result(value)

    async def call_reads(self, requests):
        """One HTTP query, with a separate signed, authorized envelope per read.

        This uses the published GraphQL adapter rather than a transaction batch.
        Missing transport support returns None so callers can retain their normal
        read path. Responses remain bounded and bound to each request alias.
        """
        require(1 <= len(requests) <= 8, 'read_batch_limit')
        for request in requests:
            self._secure_delivery(request)
            require(request.target_service == self.server, 'service_mismatch')
            require(await self._effect(request.operation) == 'read', 'read_batch_required')
        description = await self.description()
        transports = description.get('transports')
        require(transports is None or isinstance(transports, dict), 'invalid_transport_description')
        if self.name != 'http' or not transports or transports.get('graphql') != '/-/graphql':
            return None
        parameters = ','.join(f'$p{index}: JSON!' for index in range(len(requests)))
        fields = ' '.join(f'r{index}: call(packet: $p{index})' for index in range(len(requests)))
        result = await self._json(
            'POST',
            '/_read/graphql',
            body={
                'query': f'query MsgReads({parameters}) {{ {fields} }}',
                'variables': {f'p{index}': wire(request) for index, request in enumerate(requests)},
            },
            read_only=True,
        )
        require(isinstance(result, dict), 'invalid_graphql_result')
        if result.get('status') == 'error':
            # Older routes can use the ordinary HTTP failure envelope.
            # Never echo its free text or treat an authority failure as support.
            error = result.get('error')
            require(isinstance(error, dict), 'invalid_graphql_result')
            retryable = error.get('retryable', False)
            require(type(retryable) is bool, 'invalid_graphql_result')
            raise Failure(safe_error_code(error.get('code'), 'graphql_error'), retryable=retryable)
        errors = result.get('errors')
        require(errors is None or isinstance(errors, list), 'invalid_graphql_result')
        if errors:
            require(isinstance(errors[0], dict), 'invalid_graphql_result')
            extensions = errors[0].get('extensions')
            code = extensions.get('code') if isinstance(extensions, dict) else None
            raise Failure(safe_error_code(code, 'graphql_error'))
        data = result.get('data')
        require(
            isinstance(data, dict) and set(data) == {f'r{i}' for i in range(len(requests))},
            'invalid_graphql_result',
        )
        values = []
        for index, request in enumerate(requests):
            value = decode_result(data[f'r{index}'])
            require(
                value.request_id == request.request_id
                and value.operation == request.operation
                and (value.status != 'ok' or value.subject == request.subject)
                and not value.replayed,
                'invalid_graphql_result',
            )
            values.append(value)
        return values

    async def close(self):
        if self._owns_http:
            await self.http.aclose()


class PathGETTransport(HTTPTransport):
    name = 'path_get'

    async def call(self, request):
        require_url_safe_packet(request)
        effect = await self._effect(request.operation)
        raw = canonical(request)
        encoded, encoding = b64(raw), 'j'
        compressed = b64(gzip.compress(raw, mtime=0))
        if len(compressed) + 1 < len(encoded):
            encoded, encoding = compressed, 'gz'
        path = '/-/g/' + request.operation + '/' + encoding + '/' + encoded
        limits = await self.discover()
        require(len(path.encode()) <= limits.max_path_bytes, 'path_too_large')
        return decode_result(await self._json('GET', path, read_only=effect == 'read'))


class GraphQLTransport(HTTPTransport):
    name = 'graphql'

    async def call(self, request):
        self._secure_delivery(request)
        effect = await self._effect(request.operation)
        kind = 'query' if effect == 'read' else 'mutation'
        payload = {
            'query': kind + ' MsgOperation($packet: JSON!) { call(packet: $packet) }',
            'variables': {'packet': wire(request)},
        }
        endpoint = '/_read/graphql' if effect == 'read' else '/-/graphql'
        result = await self._json('POST', endpoint, body=payload, read_only=effect == 'read')
        require(isinstance(result, dict), 'invalid_graphql_result')
        errors = result.get('errors')
        require(errors is None or isinstance(errors, list), 'invalid_graphql_result')
        if errors:
            require(
                isinstance(errors, list) and isinstance(errors[0], dict), 'invalid_graphql_result'
            )
            extensions = errors[0].get('extensions')
            code = extensions.get('code') if isinstance(extensions, dict) else None
            raise Failure(safe_error_code(code, 'graphql_error'))
        data = result.get('data')
        require(
            isinstance(data, dict) and isinstance(data.get('call'), dict), 'invalid_graphql_result'
        )
        return decode_result(data['call'])


class MCPHTTPTransport(HTTPTransport):
    name = 'mcp_http'

    async def call(self, request):
        self._secure_delivery(request)
        effect = await self._effect(request.operation)
        from msg.transports.mcp_protocol import PROTOCOL_VERSION

        tool_name = (
            request.operation
            if request.contract_version == 1
            else (f'{request.operation}@{request.contract_version}')
        )
        value = await self._json(
            'POST',
            '/-/mcp',
            body={
                'jsonrpc': '2.0',
                'id': request.request_id,
                'method': 'tools/call',
                'params': {'name': tool_name, 'arguments': {'packet': wire(request)}},
            },
            headers={
                'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': PROTOCOL_VERSION,
            },
            read_only=effect == 'read',
        )
        require(isinstance(value, dict), 'invalid_mcp_result')
        if 'error' in value:
            error = value['error']
            code = error.get('message') if isinstance(error, dict) else None
            raise Failure(safe_error_code(code, 'mcp_error'))
        result = value.get('result')
        require(
            isinstance(result, dict) and isinstance(result.get('structuredContent'), dict),
            'invalid_mcp_result',
        )
        return decode_result(result['structuredContent'])


TRANSPORTS = {
    'http': HTTPTransport,
    'path_get': PathGETTransport,
    'graphql': GraphQLTransport,
    'mcp_http': MCPHTTPTransport,
}
