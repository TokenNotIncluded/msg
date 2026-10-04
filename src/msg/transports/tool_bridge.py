"""Private file exchange between a local signer and an agent's MSG MCP connector.

No network client, key material, bearer token or Exa crawler is used here.
The agent submits each packet to the pinned MSG connector and writes its full
result back. The surrounding agent runtime performs those tool calls.
"""

import asyncio
import time
from pathlib import Path

from msg.atomic_file import durable_write
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import require
from msg.core.models import TransportLimits
from msg.paths import private_directory
from msg.service_origin import service_origin
from msg.transports.packet import decode_result, require_url_safe_packet


class ToolBridgeTransport:
    name = 'tool_bridge'

    def __init__(self, server, *, exchange_dir, timeout=120):
        self.server = service_origin(server)
        self.directory = private_directory(Path(exchange_dir))
        self.timeout = timeout

    async def call(self, packet):
        require(packet.target_service == self.server, 'service_mismatch')
        # This helper-only bridge never exports reusable credential secrets.
        require_url_safe_packet(packet)
        require(len(canonical(packet)) <= 1048576, 'request_too_large')
        identifier = digest(packet).removeprefix('sha256:')
        request = self.directory / (identifier + '.request.json')
        response = self.directory / (identifier + '.response.json')
        name = (
            packet.operation
            if packet.contract_version == 1
            else f'{packet.operation}@{packet.contract_version}'
        )
        durable_write(
            request,
            canonical({
                'exchange_id': identifier,
                'server': self.server,
                'connector': self.server + '/-/mcp',
                'tool': name,
                'arguments': {'packet': wire(packet)},
                'response_file': str(response),
            }),
            mode=0o600,
        )
        deadline = time.monotonic() + self.timeout
        while not response.exists():
            require(time.monotonic() < deadline, 'tool_exchange_timeout', retryable=True)
            await asyncio.sleep(0.1)
        require(not response.is_symlink(), 'unsafe_tool_response')
        with response.open('rb') as stream:
            raw = stream.read(1048577)
        require(len(raw) <= 1048576, 'response_too_large')
        result = loads(raw)
        require(
            isinstance(result, dict)
            and result.get('exchange_id') == identifier
            and result.get('server') == self.server,
            'tool_response_mismatch',
        )
        value = decode_result(result.get('result'))
        require(
            value is not None
            and value.request_id == packet.request_id
            and value.operation == packet.operation,
            'tool_response_mismatch',
        )
        return value

    async def discover(self):
        return TransportLimits(
            max_request_bytes=1048576,
            max_response_bytes=1048576,
            max_path_bytes=None,
            encodings=frozenset({'json'}),
        )

    async def call_reads(self, requests):
        return None

    async def close(self):
        pass
