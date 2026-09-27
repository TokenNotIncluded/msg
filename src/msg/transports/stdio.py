"""Local MCP is a signed network client, never a msgd administrative proxy."""
from __future__ import annotations
import asyncio
import os
import sys
from types import SimpleNamespace

from msg.core.codec import canonical,loads
from msg.core.cursors import CursorCodec
from msg.core.errors import Failure
from msg.transports.mcp import MCPServer


async def serve_stdio(client,input_stream=None,output_stream=None):
    input_stream=input_stream or sys.stdin.buffer
    output_stream=output_stream or sys.stdout.buffer
    registry=await client.contract_registry()
    service=SimpleNamespace(registry=registry,cursors=CursorCodec(os.urandom(32)))
    server=MCPServer(service,local_client=client)
    limit=(await client.transport.discover()).max_request_bytes
    while True:
        line=await asyncio.to_thread(input_stream.readline,limit+2)
        if not line:
            break
        try:
            if len(line)>limit or not line.endswith(b'\n'):
                # Framing cannot be recovered safely from an unterminated oversized line.
                error={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'message_too_large'}}
                output_stream.write(canonical(error)+b'\n');output_stream.flush()
                break
            result=await server.handle(loads(line))
        except Failure as exc:
            result={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':exc.code}}
        if result is not None:
            output_stream.write(canonical(result)+b'\n')
            output_stream.flush()
