"""面向 agent 的 MCP 入口；封包和凭据仅在服务器内部使用。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from msg import __version__
from msg.core.codec import digest, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.core.schema_policy import local_validator
from msg.transports.mcp import MCPServer
from msg.transports.mcp_protocol import PROTOCOL_VERSION, SUPPORTED_VERSIONS
from msg.transports.mcp_tools import SCHEMAS, run_tool, tool_catalog

_ERRORS = {
    'authentication_required': (
        'MSG identity is not connected.',
        'Connect an MSG identity with msg_connect, then retry.',
    ),
    'credential_ceiling': (
        'This connection does not have permission for this action.',
        'Reconnect and approve the required MSG scope. Existing credentials are not expanded.',
    ),
    'mcp_session_required': (
        'An initialized MCP session is required for this action.',
        'The MCP client must initialize and retain the MCP-Session-Id response header.',
    ),
    'jsonrpc_id_conflict': (
        'This request ID was already used with different arguments.',
        'Retry unchanged requests with the same ID; use a new ID for a new action.',
    ),
    'payload_digest_mismatch': (
        'The request envelope digest is invalid.',
        'Use the MSG high-level tools; the SDK generates the envelope digest.',
    ),
    'invalid_jsonrpc_params': (
        'The tool arguments do not match the input schema.',
        'Supply only the fields listed in this tool schema.',
    ),
    'mcp_session_invalid': (
        'This MCP session is invalid or belongs to a different connection.',
        'Initialize a new MCP session with the current authorized identity.',
    ),
    'mcp_session_expired': (
        'This MCP session has expired.',
        'Initialize a new MCP session before starting a new action.',
    ),
}


def tool_error(code):
    message, hint = _ERRORS.get(
        code,
        ('MSG could not complete this action.', 'Check the resource and connection permissions.'),
    )
    return {
        'status': 'error',
        'error': {'code': code, 'message': message, 'hint': hint, 'retryable': False},
    }


@dataclass
class _Call:
    arguments_digest: str
    packets: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentMCPServer:
    def __init__(self, service):
        self.service = service
        self.raw = MCPServer(service)
        self._calls = OrderedDict()

    def _session(self, identity, session_id, *, initialize=False):
        binding = identity.session_binding if identity else None
        now = self.service.clock()
        if session_id:
            try:
                position = self.service.cursors.decode(
                    session_id, 'mcp-session', {'service': self.service.settings.service_url}
                )
                require(
                    isinstance(position, dict)
                    and isinstance(position.get('nonce'), str)
                    and position.get('binding') in (None, binding),
                    'mcp_session_invalid',
                )
                require(now < parse_time(position['expires_at']), 'mcp_session_expired')
            except KeyError, TypeError, ValueError:
                raise Failure('mcp_session_invalid') from None
            except Failure as exc:
                if exc.code not in {'mcp_session_invalid', 'mcp_session_expired'}:
                    raise Failure('mcp_session_invalid') from None
                raise
        elif initialize:
            position = {'nonce': uuid4().hex, 'expires_at': wire(now + timedelta(hours=24))}
        else:
            return None, None
        # Anonymous discovery may precede OAuth. Upgrade that session once the
        # host sends a bearer, while isolating retries by the OAuth family.
        position['binding'] = binding
        value = self.service.cursors.encode(
            'mcp-session', {'service': self.service.settings.service_url}, position
        )
        return position['nonce'], value

    @staticmethod
    def _scopes(name):
        if name == 'msg_connect':
            return ('msg.mcp.read', 'msg.mcp.message', 'msg.mcp.post')
        if name in {'msg_send', 'msg_dm_decide', 'msg_ack'}:
            return ('msg.mcp.read', 'msg.mcp.message')
        if name == 'msg_reply':
            return ('msg.mcp.read', 'msg.mcp.message', 'msg.mcp.post')
        if name == 'msg_post':
            return ('msg.mcp.read', 'msg.mcp.post')
        return ('msg.mcp.read',)

    def _error_result(self, code, name=None):
        value = tool_error(code)
        result = {
            'content': [{'type': 'text', 'text': value['error']['message']}],
            'structuredContent': value,
            'isError': True,
        }
        if code in {'authentication_required', 'credential_ceiling'}:
            from msg.transports.mcp_auth import challenge

            result['_meta'] = {
                'mcp/www_authenticate': [
                    challenge(
                        self.service,
                        'insufficient_scope' if code == 'credential_ceiling' else None,
                        self._scopes(name),
                    )
                ]
            }
        return result

    async def _call(self, params, rpc_id, identity, nonce):
        name = params['name']
        arguments = params.get('arguments', {})
        require(isinstance(arguments, dict), 'invalid_jsonrpc_params')
        catalog = tool_catalog(self.service.registry, identity)
        tool = next((item for item in catalog if item['name'] == name), None)
        if tool is None:
            require(name in SCHEMAS, 'unknown_tool')
            require(identity is not None, 'authentication_required')
            raise Failure('credential_ceiling')
        require(local_validator(tool['inputSchema']).is_valid(arguments), 'invalid_jsonrpc_params')
        if name == 'msg_connect':
            require(identity is not None, 'authentication_required')
        mutating = not tool.get('annotations', {}).get('readOnlyHint', False)
        require(not mutating or nonce is not None, 'mcp_session_required')
        # Never use a model-supplied ID as a global account replay key.
        binding = identity.session_binding if identity else 'anonymous'
        key = (binding, nonce or uuid4().hex, type(rpc_id).__name__, rpc_id)
        fingerprint = digest((name, arguments))
        existing = self._calls.get(key)
        if existing:
            require(existing.arguments_digest == fingerprint, 'jsonrpc_id_conflict')
            self._calls.move_to_end(key)
        else:
            existing = _Call(fingerprint)
            self._calls[key] = existing
            if len(self._calls) > 256:
                self._calls.popitem(last=False)
        async with existing.lock:
            index = 0

            async def call(operation, business_arguments, *, contract_version=1):
                nonlocal index
                slot = index
                index += 1
                spec = self.service.registry.operation(operation, contract_version)
                require('network' in spec.entries, 'entry_not_allowed')
                request_id = 'mcp_' + digest((*key, slot)).removeprefix('sha256:')
                description = digest((operation, contract_version, business_arguments))
                cached = existing.packets.get(slot)
                require(cached is None or cached == description, 'jsonrpc_id_conflict')
                existing.packets[slot] = description
                packet = request_for(
                    operation,
                    business_arguments,
                    self.service.settings.service_url,
                    subject=identity.subject if identity else None,
                    token=(identity.credential_id, identity.token) if identity else None,
                    request_id=request_id,
                    expires_at=self.service.clock() + timedelta(seconds=180),
                    source='mcp',
                    contract_version=contract_version,
                )
                return await self.service.executor.execute(packet, entry='network')

            value = await run_tool(
                'msg_me' if name == 'msg_connect' else name,
                arguments,
                call=call,
                identity=identity,
            )
        result = {
            'content': [{'type': 'text', 'text': 'MSG result is available in structuredContent.'}],
            'structuredContent': value,
            'isError': value.get('status') == 'error',
        }
        if result['isError']:
            code = value.get('error', {}).get('code')
            if code in {'authentication_required', 'credential_ceiling'}:
                return self._error_result(code, name)
        return result

    async def handle(self, message, *, identity=None, session_id=None):
        """Return JSON-RPC output and transport headers, without credential data."""
        rpc_id = message.get('id') if isinstance(message, dict) else None
        headers = {'MCP-Protocol-Version': PROTOCOL_VERSION}
        try:
            require(
                isinstance(message, dict)
                and message.get('jsonrpc') == '2.0'
                and isinstance(message.get('method'), str),
                'invalid_jsonrpc',
            )
            require(rpc_id is None or type(rpc_id) in (str, int), 'invalid_jsonrpc_id')
            require(not isinstance(rpc_id, str) or len(rpc_id) <= 128, 'invalid_jsonrpc_id')
            params = message.get('params', {})
            require(isinstance(params, dict), 'invalid_jsonrpc_params')
            if rpc_id is None:
                return None, headers
            method = message['method']
            # SDK/Agent Link clients retain their exact signed wire contract.
            # It is accepted for compatibility but absent from the default list.
            if method == 'tools/call':
                require(isinstance(params.get('name'), str), 'invalid_jsonrpc_params')
            if method == 'tools/call' and not params['name'].startswith('msg_'):
                return await self.raw.handle(message), headers
            nonce, session = self._session(identity, session_id, initialize=method == 'initialize')
            if session:
                headers['MCP-Session-Id'] = session
            if method == 'initialize':
                requested = params.get('protocolVersion')
                selected = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
                result = {
                    'protocolVersion': selected,
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'msg', 'version': __version__},
                    'instructions': (
                        'Use msg_me to check your identity and msg_connect to connect. '
                        'Use the MSG tools with ordinary arguments; MSG constructs and '
                        'authenticates requests. Reading messages does not acknowledge them. '
                        'Explicit advanced SDK tools are available at /-/mcp/raw.'
                    ),
                }
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                require(set(params) <= {'cursor', '_meta'}, 'invalid_jsonrpc_params')
                require(params.get('cursor') is None, 'invalid_cursor')
                result = {'tools': tool_catalog(self.service.registry, identity)}
            elif method == 'tools/call':
                require(
                    set(params) <= {'name', 'arguments', '_meta'}
                    and isinstance(params.get('name'), str),
                    'invalid_jsonrpc_params',
                )
                try:
                    result = await self._call(params, rpc_id, identity, nonce)
                except Failure as exc:
                    result = self._error_result(exc.code, params['name'])
            else:
                return {
                    'jsonrpc': '2.0',
                    'id': rpc_id,
                    'error': {'code': -32601, 'message': 'method_not_found'},
                }, headers
            return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}, headers
        except Failure as exc:
            if rpc_id is None:
                return None, headers
            return {
                'jsonrpc': '2.0',
                'id': rpc_id,
                'error': {'code': -32602, 'message': exc.code},
            }, headers
