"""MCP 2025-11-25 JSON-RPC over stateless Streamable HTTP or local stdio.

Remote calls carry the same signed operation envelope as other transports.
Local stdio exposes argument schemas directly and delegates signing to msg.
No root-administration operation is generated as an MCP tool.
"""
from __future__ import annotations
from dataclasses import replace
from msg import __version__
from msg.core.codec import canonical,digest,loads
from msg.core.errors import Failure,require
from msg.core.executor import result_wire
from msg.transports.packet import decode_packet,REQUEST_SCHEMA,RESULT_SCHEMA

PROTOCOL_VERSION='2025-11-25'
SUPPORTED_VERSIONS=frozenset({PROTOCOL_VERSION,'2025-06-18','2025-03-26'})


class MCPServer:
    def __init__(self,service, *, local_client=None):
        self.service=service
        self.local_client=local_client
        self._requests={}

    async def tools(self,cursor=None):
        registry=self.service.registry
        specs=registry.operations('network')
        offset=0
        if cursor:
            offset=self.service.cursors.decode(cursor,'mcp-tools',registry.catalog()['digest'])
            require(type(offset) is int and 0<=offset<=len(specs),'invalid_cursor')
        if self.local_client is not None:
            await registry.load_schemas(specs[offset:offset+8])
        tools=[]
        # Bounded discovery; an agent can request only the next useful page.
        for spec in specs[offset:offset+8]:
            if self.local_client is not None:
                input_schema=registry.schema(spec.input_schema)
            else:
                packet_schema={**REQUEST_SCHEMA,'properties':{**REQUEST_SCHEMA['properties'],
                    'operation':{'const':spec.name},'contract_version':{'const':spec.version},
                    'arguments':registry.schema(spec.input_schema)}}
                input_schema={'type':'object','properties':{'packet':packet_schema},'required':['packet'],'additionalProperties':False}
            tools.append({'name':spec.name,'description':f'{spec.name}@{spec.version}; {spec.effect}; contract /!{spec.name}/schema',
                'inputSchema':input_schema,'outputSchema':RESULT_SCHEMA,
                'annotations':{'readOnlyHint':spec.effect=='read','idempotentHint':True,
                    'destructiveHint':spec.effect!='read','openWorldHint':spec.effect=='external'}})
        result={'tools':tools}
        if offset+len(tools)<len(specs):
            result['nextCursor']=self.service.cursors.encode('mcp-tools',registry.catalog()['digest'],offset+len(tools))
        return result

    async def handle(self,message):
        id=message.get('id') if isinstance(message,dict) else None
        try:
            require(isinstance(message,dict) and message.get('jsonrpc')=='2.0' and
                    isinstance(message.get('method'),str),'invalid_jsonrpc')
            require(id is None or type(id) in (str,int),'invalid_jsonrpc_id')
            require(not isinstance(id,str) or len(id)<=128,'invalid_jsonrpc_id')
            method=message['method']
            params=message.get('params',{})
            require(isinstance(params,dict),'invalid_jsonrpc_params')
            if id is None:
                # Notifications never execute operations, even if their method is
                # tools/call. This prevents unacknowledged mutation messages.
                return None
            if method=='initialize':
                requested=params.get('protocolVersion')
                selected=requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
                result={'protocolVersion':selected,'capabilities':{'tools':{'listChanged':False}},
                        'serverInfo':{'name':'msg.lmm.best','version':__version__},
                        'instructions':'Read /rules. Network operations use one signed envelope. Root administration is not available.'}
            elif method=='ping':
                result={}
            elif method=='tools/list':
                require(set(params)<={'cursor','_meta'},'invalid_jsonrpc_params')
                result=await self.tools(params.get('cursor'))
            elif method=='tools/call':
                require(set(params)<={'name','arguments','_meta'} and isinstance(params.get('name'),str),'invalid_jsonrpc_params')
                spec=self.service.registry.operation(params['name'])
                require('network' in spec.entries,'entry_not_allowed')
                arguments=params.get('arguments',{})
                if self.local_client is None:
                    require(isinstance(arguments,dict) and set(arguments)=={'packet'},'signed_packet_required')
                    packet=decode_packet(arguments['packet'],self.service.settings.server.limits.max_request_bytes)
                    require(packet.operation==spec.name,'operation_mismatch')
                    output=await self.service.executor.execute(packet,entry='network')
                else:
                    require(isinstance(arguments,dict),'invalid_jsonrpc_params')
                    metadata=params.get('_meta',{})
                    expected=metadata.get('msg/expected_generations',())
                    request_id=metadata.get('msg/request_id')
                    cache_key=(type(id).__name__,id)
                    request_digest=digest((params['name'],arguments,expected))
                    existing=self._requests.get(cache_key)
                    if existing:
                        require(existing[0]==request_digest,'jsonrpc_id_conflict')
                        request_id=existing[1]
                    packet=self.local_client.prepare(params['name'],arguments,expected=expected,request_id=request_id)
                    self._requests[cache_key]=(request_digest,packet.request_id)
                    # This is a process-local replay cache, not per-account server state.
                    if len(self._requests)>256:
                        self._requests.pop(next(iter(self._requests)))
                    output=await self.local_client.send(packet)
                value=result_wire(output)
                encoded=canonical(value).decode()
                # Structured content is authoritative. Avoid doubling large
                # transfer chunks in the text compatibility projection.
                text=encoded if len(encoded)<=8192 else canonical({'request_id':output.request_id,
                    'status':output.status,'structured_content':True}).decode()
                result={'content':[{'type':'text','text':text}],
                        'structuredContent':value,'isError':output.status=='error'}
            else:
                return {'jsonrpc':'2.0','id':id,'error':{'code':-32601,'message':'method_not_found'}}
            return {'jsonrpc':'2.0','id':id,'result':result}
        except Failure as exc:
            if id is None:
                return None
            return {'jsonrpc':'2.0','id':id,'error':{'code':-32602,'message':exc.code}}
