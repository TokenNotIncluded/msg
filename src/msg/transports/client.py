"""Client-side envelopes; transport selection never changes business semantics."""
from __future__ import annotations

import gzip
from urllib.parse import urlsplit
import httpx

from msg.core.codec import canonical, b64, loads, wire, decode
from msg.core.errors import Failure, require
from msg.core.models import TransportLimits
from msg.transports.packet import decode_result


class HTTPTransport:
    name = 'http'

    def __init__(self, server, *, http=None, max_response_bytes=1048576, max_path_bytes=8192):
        server = server.rstrip('/')
        parsed = urlsplit(server)
        require(parsed.scheme in {'https','http'} and parsed.hostname and not parsed.username
                and not parsed.query and not parsed.fragment and parsed.path in {'','/'}, 'invalid_server_url')
        self.server = server
        self.http = http or httpx.AsyncClient(base_url=server, timeout=30, follow_redirects=False)
        self._owns_http = http is None
        self.max_response_bytes = max_response_bytes
        self.max_path_bytes = max_path_bytes
        self._description = None
        self.calls = 0
        self.bytes_received = 0
        self.bytes_sent = 0

    async def _json(self, method, path, *, body=None, headers=None, maximum=None):
        require(path.startswith('/') and not path.startswith('//'), 'invalid_relative_endpoint')
        limit = maximum or self.max_response_bytes
        raw = canonical(body) if body is not None else None
        headers = dict(headers or {})
        if raw is not None:
            headers.setdefault('Content-Type','application/json')
        self.calls += 1
        self.bytes_sent += len(raw or b'') + len(path.encode())
        # Never follow a credential-bearing operation to another origin.
        async with self.http.stream(method, self.server + path, content=raw, headers=headers,
                                    follow_redirects=False) as response:
            require(not response.is_redirect, 'redirect_not_allowed')
            data = bytearray()
            async for piece in response.aiter_bytes():
                require(len(data)+len(piece)<=limit, 'response_too_large')
                data.extend(piece)
            self.bytes_received += len(data)
            if response.status_code==202 and not data:
                return None
            try:
                value = loads(bytes(data))
            except Failure:
                raise Failure('invalid_server_response') from None
            if response.status_code>=400 and 'status' not in value:
                code = value.get('error',{}).get('code','transport_error') if isinstance(value,dict) else 'transport_error'
                raise Failure(code,retryable=response.status_code in {502,503,504})
            return value

    async def description(self):
        if self._description is None:
            value = await self._json('GET','/_transports')
            require(value.get('version')==1 and value.get('target_service')==self.server, 'service_mismatch')
            require(isinstance(value.get('operations'),dict), 'invalid_transport_description')
            self._description = value
        return self._description

    async def discover(self):
        value = await self.description()
        limits = dict(value['limits'])
        limits['max_response_bytes'] = min(limits['max_response_bytes'],self.max_response_bytes)
        limits['max_path_bytes'] = min(limits['max_path_bytes'],self.max_path_bytes)
        return decode(TransportLimits,limits)

    async def _effect(self, name):
        description = await self.description()
        effect = description['operations'].get(name)
        require(effect in {'read','transaction','external'}, 'unknown_operation')
        return effect

    async def call(self, request):
        await self._effect(request.operation)
        path = '/-/p/' + request.operation
        # Signed reads use POST to a query endpoint: read/write is the operation's
        # declared effect, not inferred from whether HTTP has a request body.
        value = await self._json('POST',path,body=wire(request))
        return decode_result(value)

    async def close(self):
        if self._owns_http:
            await self.http.aclose()


class PathGETTransport(HTTPTransport):
    name = 'path_get'

    async def call(self, request):
        await self._effect(request.operation)
        raw = canonical(request)
        encoded, encoding = b64(raw), 'j'
        compressed = b64(gzip.compress(raw,mtime=0))
        if len(compressed)+1<len(encoded):
            encoded, encoding = compressed,'gz'
        path = '/-/g/'+request.operation+'/'+encoding+'/'+encoded
        limits = await self.discover()
        require(len(path.encode())<=limits.max_path_bytes,'path_too_large')
        return decode_result(await self._json('GET',path))


class GraphQLTransport(HTTPTransport):
    name = 'graphql'

    async def call(self, request):
        effect = await self._effect(request.operation)
        kind = 'query' if effect=='read' else 'mutation'
        payload = {'query':kind+' MsgOperation($packet: JSON!) { call(packet: $packet) }',
                   'variables':{'packet':wire(request)}}
        endpoint = '/_read/graphql' if effect=='read' else '/-/graphql'
        result = await self._json('POST',endpoint,body=payload)
        if result.get('errors'):
            raise Failure(result['errors'][0].get('extensions',{}).get('code','graphql_error'))
        require(isinstance(result.get('data',{}).get('call'),dict),'invalid_graphql_result')
        return decode_result(result['data']['call'])


class MCPHTTPTransport(HTTPTransport):
    name = 'mcp_http'

    async def call(self, request):
        await self._effect(request.operation)
        from msg.transports.mcp import PROTOCOL_VERSION
        value = await self._json('POST','/-/mcp',body={'jsonrpc':'2.0','id':request.request_id,
            'method':'tools/call','params':{'name':request.operation,'arguments':{'packet':wire(request)}}},
            headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':PROTOCOL_VERSION})
        if 'error' in value:
            raise Failure(value['error'].get('message','mcp_error'))
        require(isinstance(value.get('result',{}).get('structuredContent'),dict),'invalid_mcp_result')
        return decode_result(value['result']['structuredContent'])


TRANSPORTS = {'http':HTTPTransport,'path_get':PathGETTransport,'graphql':GraphQLTransport,'mcp_http':MCPHTTPTransport}
