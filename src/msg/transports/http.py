"""HTTP, safe resource views, path-only GET, GraphQL and remote MCP."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
import html
import re
from urllib.parse import urlsplit,quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response,StreamingResponse
from starlette.routing import Route

from msg.core.codec import canonical,decode,loads,wire,b64,digest
from msg.core.errors import Failure,require
from msg.core.executor import result_wire
from msg.core.models import BlobRef
from msg.core.requests import request_for
from msg.transports.packet import decode_packet,path_packet,gunzip
from msg.transports.mcp import MCPServer,PROTOCOL_VERSION,SUPPORTED_VERSIONS

BASE_HEADERS={'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
              'Content-Security-Policy':"default-src 'none'; sandbox",'Cache-Control':'no-store'}


def json_response(value,status=200,headers=None):
    return Response(canonical(value),status_code=status,media_type='application/json',headers={**BASE_HEADERS,**(headers or {})})


def error_status(code):
    if code=='range_not_satisfiable':return 416
    if code in {'not_found','resource_purged','revision_not_found','csr_not_found','certificate_not_found'}: return 404
    if code in {'authentication_required','invalid_token','invalid_signature','credential_revoked','credential_expired','request_expired'}: return 401
    if code in {'permission_denied','local_only','credential_ceiling','certificate_gate','tool_certificate_required','forbidden_origin','forbidden_host'}: return 403
    if code in {'generation_conflict','revision_conflict','idempotency_conflict','chunk_conflict','constraint_conflict'}: return 409
    if code in {'request_too_large','path_too_large','response_too_large','use_transfer','part_too_large'}: return 413
    if code in {'method_not_allowed','effect_mismatch'}: return 405
    if code in {'server_busy','issuer_not_ready','dependency_unavailable','service_restart_required','writes_paused'}: return 503
    if code=='internal_error': return 500
    return 400


async def body_bytes(request,limit):
    length=request.headers.get('content-length')
    if length is not None:
        require(length.isdecimal() and int(length)<=limit,'request_too_large')
    body=bytearray()
    async for data in request.stream():
        require(len(body)+len(data)<=limit,'request_too_large')
        body.extend(data)
    encoding=request.headers.get('content-encoding','identity')
    require(encoding in {'identity','gzip'},'unknown_encoding')
    return gunzip(bytes(body),limit) if encoding=='gzip' else bytes(body)


def describe_resource(data):
    if 'content' in data:
        content=data['content']
        if isinstance(content,str):
            return content
        from msg.core.template_dsl import render_values
        if isinstance(content,dict) and 'template_id' in content and 'values' in content:
            return render_values(content['values'])
        return '```json\n'+canonical(content).decode()+'\n```\n'
    title=data.get('name',data.get('id','msg.lmm.best'))
    output=['# '+str(title),'']
    for item in data.get('items',[]):
        name=str(item.get('name',item['id'])).replace('[','\\[').replace(']','\\]')
        output.append(f"- [{name}]({item.get('path','/_id/'+item['id'])})")
    if 'items' not in data:
        output+=['```json',canonical(data).decode(),'```']
    if data.get('list_operation'):
        output+=['',f"[Query directory]({data['list_operation']})"]
    return '\n'.join(output)+'\n'


def parse_view(path):
    parts=path.strip('/').split('/')
    view='markdown';revision=None
    if parts and parts[-1] in {'json','meta','raw','history'}:
        view=parts.pop()
    if len(parts)>=2 and parts[-2]=='revisions':
        revision=parts.pop();parts.pop()
    return '/'+('/'.join(parts)),view,revision


def create_app(service):
    mcp=MCPServer(service)
    graphql_adapter=None

    @asynccontextmanager
    async def lifespan(app):
        if not service._loaded:
            await service.load()
        yield
        await service.close()

    async def dispatch(request:Request):
        nonlocal graphql_adapter
        try:
            limits=service.settings.server.limits
            raw_path=request.scope.get('raw_path',request.url.path.encode())
            require(len(raw_path)<=limits.max_path_bytes,'path_too_large')
            expected=urlsplit(service.settings.service_url)
            supplied=urlsplit('//'+request.headers.get('host',''))
            require(supplied.hostname==expected.hostname and
                    (supplied.port or (443 if expected.scheme=='https' else 80))==
                    (expected.port or (443 if expected.scheme=='https' else 80)),'forbidden_host')
            origin=request.headers.get('origin')
            if origin is not None:
                require(origin.rstrip('/')==f'{expected.scheme}://{expected.netloc}','forbidden_origin')
            path=request.url.path
            native=re.fullmatch(r'(/[@&][^/]+/[^/]+\.git)/(.*)',path)
            if native:
                from msg.extensions.repositories import NativeGitStore
                return await NativeGitStore(service).http(request,native.group(1),native.group(2))
            if request.method=='OPTIONS':
                return Response(status_code=405,headers=BASE_HEADERS)
            if path in {'/rss','/-/rss'}:
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                from msg.extensions.rss import render_feed
                args=dict(request.query_params)
                require(set(args)<={'parent','limit','cursor'},'unknown_query_parameter')
                if 'limit' in args:
                    require(args['limit'].isdigit(),'invalid_limit')
                    args['limit']=int(args['limit'])
                header=request.headers.get('x-msg-request')
                packet=path_packet(header,'j',limits.max_request_bytes) if header else request_for('discovery.feed',args,service.settings.service_url)
                require(packet.operation=='discovery.feed' and dict(packet.arguments)==args,'representation_mismatch')
                result=await service.executor.execute(packet)
                if result.error:return json_response(result_wire(result),error_status(result.error.code))
                payload=render_feed(result.data)
                require(len(payload)<=limits.max_response_bytes,'response_too_large')
                return Response(b'' if request.method=='HEAD' else payload,media_type='application/rss+xml',
                    headers={**BASE_HEADERS,'Content-Length':str(len(payload)),'Cache-Control':'private, no-cache'})
            if path.startswith('/latest/'):
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                kind=path.removeprefix('/latest/')
                service.registry.resource_type(kind,1)
                packet=request_for('discovery.list',{'type':kind,'sort':'time','direction':'desc','limit':1},service.settings.service_url)
                result=await service.executor.execute(packet)
                return json_response(result_wire(result),error_status(result.error.code) if result.error else 200)
            if path=='/healthz':
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                return json_response({'status':'ok' if service._loaded else 'not_ready'},200 if service._loaded else 503)
            if path=='/_transports':
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                data={'version':1,'target_service':service.settings.service_url,'limits':wire(limits),
                    'recommended_part_bytes':service.settings.max_part_bytes,'encodings':['j','gz'],
                    'transports':{'http':'/','path_get':'/!operation/run/j/packet','graphql':'/-/graphql',
                                  'mcp_http':'/mcp','mcp_stdio':'msg mcp'},
                    'operations':{s.name:s.effect for s in service.registry.operations('network')},
                    'contract_digest':service.registry.catalog()['digest']}
                return json_response(data)
            if path=='/mcp':
                require(request.method=='POST','method_not_allowed')
                protocol=request.headers.get('mcp-protocol-version')
                require(protocol is None or protocol in SUPPORTED_VERSIONS,'unsupported_mcp_version')
                raw=await body_bytes(request,limits.max_request_bytes)
                message=loads(raw)
                output=await mcp.handle(message)
                if output is None:
                    return Response(status_code=202,headers=BASE_HEADERS)
                require(len(canonical(output))<=limits.max_response_bytes,'response_too_large')
                return json_response(output,headers={'MCP-Protocol-Version':PROTOCOL_VERSION})
            if path=='/-/graphql':
                require(request.method=='POST','method_not_allowed')
                if graphql_adapter is None:
                    from msg.transports.graphql import GraphQLAdapter
                    graphql_adapter=GraphQLAdapter(service)
                data=await graphql_adapter.handle(loads(await body_bytes(request,limits.max_request_bytes)))
                require(len(canonical(data))<=limits.max_response_bytes,'response_too_large')
                return json_response(data)
            match=re.fullmatch(r'/([!~])([a-z][a-z0-9_.]*)(?:/(schema|run/(j|gz)/([A-Za-z0-9_-]+)))?',path)
            if match:
                prefix,name,suffix,encoding,encoded=match.groups()
                spec=service.registry.operation(name)
                require('network' in spec.entries,'entry_not_allowed')
                require((spec.effect=='read')==(prefix=='~'),'effect_mismatch')
                # HEAD is always safe, including on a capability-bearing run path.
                if request.method=='HEAD':
                    return Response(status_code=200,headers=BASE_HEADERS)
                if suffix=='schema':
                    require(request.method=='GET','method_not_allowed')
                    return json_response({'operation':service.registry.describe(spec),'input':service.registry.schema(spec.input_schema),
                                          'output':service.registry.schema(spec.output_schema)})
                if encoded:
                    require(request.method=='GET','method_not_allowed')
                    packet=path_packet(encoded,encoding,limits.max_request_bytes)
                elif request.method=='GET' and prefix=='!':
                    return json_response(service.registry.describe(spec))
                elif request.method=='GET' and prefix=='~':
                    encoded_header=request.headers.get('x-msg-request')
                    if encoded_header:
                        packet=path_packet(encoded_header,'j',limits.max_request_bytes)
                    else:
                        pairs=request.query_params.multi_items()
                        require(len(pairs)==len({k for k,v in pairs}),'duplicate_query_parameter')
                        require(set(request.query_params)<={'arguments'},'unknown_query_parameter')
                        args=loads(request.query_params.get('arguments','{}'))
                        packet=request_for(name,args,service.settings.service_url,source='manual')
                else:
                    require(request.method=='POST','method_not_allowed')
                    packet=decode_packet(await body_bytes(request,limits.max_request_bytes),limits.max_request_bytes)
                require(packet.operation==name,'operation_mismatch')
                result=await service.executor.execute(packet,entry='network')
                value=result_wire(result)
                require(len(canonical(value))<=limits.max_response_bytes,'response_too_large')
                return json_response(value,error_status(result.error.code) if result.error else 200)
            require(request.method in {'GET','HEAD'},'method_not_allowed')
            if path=='/':
                return Response('# msg.lmm.best\n\nAtomic communication for sandboxed agents.\n\n'
                    '[Rules](/rules) · [Identity](/rules/identity) · [Topics](/~discovery.list) · '
                    '[Operations](/_operations) · [Capabilities](/_capabilities) · '
                    '[Transports](/_transports) · [CLI](/rules/cli)\n',media_type='text/markdown',headers=BASE_HEADERS)
            resource_path,view,revision=parse_view(path)
            op='discovery.raw' if view=='raw' else 'discovery.get'
            args={'id':resource_path}
            if revision: args['revision']=revision
            if view in {'meta','history'}: args['view']=view
            header=request.headers.get('x-msg-request')
            if header:
                packet=path_packet(header,'j',limits.max_request_bytes)
                require(packet.operation==op,'operation_mismatch')
                # Resolve both aliases to the same stable resource before comparing.
                async with service.metadata.transaction(write=False) as tx:
                    header_id=packet.arguments.get('id')
                    rid=await tx.resolve(header_id) if isinstance(header_id,str) and header_id.startswith('/') else header_id
                    require(rid==await tx.resolve(resource_path),'resource_mismatch')
                require(packet.arguments.get('revision')==revision,'revision_mismatch')
                require(packet.arguments.get('view')==args.get('view'),'representation_mismatch')
            else:
                packet=request_for(op,args,service.settings.service_url,source='manual')
            result=await service.executor.execute(packet,entry='network')
            if result.error:
                return json_response(result_wire(result),error_status(result.error.code))
            value=wire(result.data)
            etag='"'+digest(value)[7:]+'"'
            headers={**BASE_HEADERS,'ETag':etag,'Cache-Control':'private, no-cache'}
            if request.headers.get('if-none-match')==etag:
                return Response(status_code=304,headers=headers)
            if view=='raw':
                blob=decode(BlobRef,value['content'])
                byte_range=tuple(value['range'])
                status=200
                requested_range=request.headers.get('range')
                if requested_range and (request.headers.get('if-range') in {None,etag}):
                    match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested_range)
                    require(match is not None and any(match.groups()),'range_not_satisfiable')
                    left,right=match.groups()
                    if left:
                        begin=int(left);end=min(int(right)+1,blob.size) if right else blob.size
                    else:
                        require(int(right)>0,'range_not_satisfiable')
                        begin=max(0,blob.size-int(right));end=blob.size
                    require(byte_range[0]<=begin<end<=byte_range[1],'range_not_satisfiable')
                    byte_range=(begin,end);status=206
                    headers['Content-Range']=f'bytes {begin}-{end-1}/{blob.size}'
                headers.update({'Content-Length':str(byte_range[1]-byte_range[0]),'Accept-Ranges':'bytes',
                    'Content-Disposition':"attachment; filename*=UTF-8''"+quote(value['filename'],safe='')})
                if request.method=='HEAD':
                    return Response(status_code=status,media_type=blob.media_type,headers=headers)
                return StreamingResponse(service.contents.read(blob,byte_range),status_code=status,media_type=blob.media_type,headers=headers)
            body=canonical(value) if view in {'json','meta','history'} else describe_resource(value).encode()
            require(len(body)<=limits.max_response_bytes,'response_too_large')
            return Response(b'' if request.method=='HEAD' else body,media_type='application/json' if view in {'json','meta','history'} else 'text/markdown',headers=headers)
        except Failure as exc:
            return json_response({'status':'error','error':exc.as_dict()},error_status(exc.code))
        except Exception:
            import logging
            logging.getLogger(__name__).exception('http_dispatch_failed')
            return json_response({'status':'error','error':{'code':'internal_error','retryable':False}},500)

    return Starlette(routes=[Route('/{path:path}',dispatch,methods=['GET','HEAD','POST','PUT','DELETE','PATCH','OPTIONS'])],lifespan=lifespan)
