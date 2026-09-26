"""HTTP, safe resource views, path-only GET, GraphQL and remote MCP."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote_to_bytes, urlencode, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from msg.core.codec import canonical, decode, digest, loads, wire
from msg.core.errors import Failure, require
from msg.core.executor import result_wire
from msg.core.models import BlobRef
from msg.core.requests import request_for
from msg.core.tags import normalize_tag
from msg.transports.mcp import PROTOCOL_VERSION, SUPPORTED_VERSIONS, MCPServer
from msg.transports.packet import decode_packet, gunzip, path_packet

BASE_HEADERS={'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
              'Content-Security-Policy':"default-src 'none'; sandbox",'Cache-Control':'no-store'}
TRANSFER_OPERATIONS=frozenset({'transfer.open','transfer.part_put','transfer.part_get',
                               'transfer.status','transfer.seal','transfer.cancel'})


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


def parse_stable_view(path,raw_path):
    if not path.startswith(('/_r/','/_read/')):
        return None
    # Stable ID paths are ASCII identifiers, with no alternate percent spelling.
    try:
        require(raw_path.decode('ascii')==path and b'%' not in raw_path,'not_found')
    except UnicodeDecodeError as exc:
        raise Failure('not_found') from exc
    match=re.fullmatch(r'/_r(?:ead)?/([A-Za-z0-9_.:-]{1,160})/(json|meta|raw|history)',path)
    if match:
        rid,view=match.groups()
        return '/_id/'+rid,view,None
    match=re.fullmatch(r'/_r(?:ead)?/([A-Za-z0-9_.:-]{1,160})/rev/([A-Za-z0-9_.:-]{1,160})',path)
    require(match is not None,'not_found')
    rid,revision=match.groups()
    return '/_id/'+rid,'markdown',revision


def create_app(service):
    mcp=MCPServer(service)
    graphql_adapter=None
    short_codes=None

    @asynccontextmanager
    async def lifespan(app):
        if not service._loaded:
            await service.load()
        yield
        await service.close()

    async def dispatch(request:Request):
        nonlocal graphql_adapter, short_codes
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
            require('x-http-method-override' not in request.headers and
                    'x-method-override' not in request.headers,'method_not_allowed')
            path=request.url.path
            if (path=='/-' or path.startswith('/-/')) and not (raw_path==b'/-' or raw_path.startswith(b'/-/')):
                raise Failure('not_found')
            if raw_path.startswith(b'/-/'):
                segments=raw_path.split(b'/')
                if (any(unquote_to_bytes(segment) in {b'.',b'..'} for segment in segments) or
                        any(b'%' in segment for segment in segments[2:4]) or
                        (len(segments)>4 and segments[2] in {b'g',b'p'} and
                         b'.' in segments[3] and b'%' in segments[4])):
                    raise Failure('not_found')
            native=re.fullmatch(r'(/[@&][^/]+/[^/]+\.git)/(.*)',path)
            if native:
                require(service.registry.operation('git.refs').effect=='read','effect_mismatch')
                from msg.extensions.repositories import NativeGitStore
                return await NativeGitStore(service).http(request,native.group(1),native.group(2))
            if path.startswith(('/!','/~','/run/j/','/run/gz/')) or path=='/mcp':
                raise Failure('not_found')
            if request.method=='OPTIONS':
                return Response(status_code=405,headers=BASE_HEADERS)
            if path in {'/rss','/-/rss'}:
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                require(service.registry.operation('discovery.feed').effect=='read','effect_mismatch')
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
                require(service.registry.operation('discovery.list').effect=='read','effect_mismatch')
                kind=path.removeprefix('/latest/')
                service.registry.resource_type(kind,1)
                packet=request_for('discovery.list',{'type':kind,'sort':'time','direction':'desc','limit':1},service.settings.service_url)
                result=await service.executor.execute(packet)
                return json_response(result_wire(result),error_status(result.error.code) if result.error else 200)
            if path=='/healthz':
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                return json_response({'status':'ok' if service._loaded else 'not_ready'},200 if service._loaded else 503)
            if path in {'/_read/query','/_r/query'} or raw_path.startswith((b'/_read/c/',b'/_r/c/')):
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                continuation=raw_path.startswith((b'/_read/c/',b'/_r/c/'))
                require(not continuation or not request.url.query,'unknown_query_parameter')
                if continuation:
                    segments=raw_path.split(b'/')
                    require(len(segments)==4 and segments[2]==b'c' and
                            re.fullmatch(rb'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',segments[3]) is not None,
                            'invalid_cursor')
                    cursor=segments[3].decode('ascii')
                    query,_=service.cursors.inspect_page(cursor,service.clock())
                    operation=query.get('operation')
                    require(operation=='discovery.read_query','cursor_kind_mismatch')
                    args={'cursor':cursor}
                else:
                    pairs=request.query_params.multi_items()
                    require(len(pairs)==len({key for key,_ in pairs}),'duplicate_query_parameter')
                    query=dict(pairs)
                    require(set(query)<={'root','select','filter','sort','first','after','expand','projection'},
                            'unknown_query_parameter')
                    require(query.get('expand','none')=='none' and
                            query.get('projection','meta')=='meta','query_cost_exceeded')
                    if 'after' in query:
                        require(set(query)=={'after'},'cursor_query_mismatch')
                        cursor=query['after']
                        saved,_=service.cursors.inspect_page(cursor,service.clock())
                        operation=saved.get('operation')
                        require(operation=='discovery.read_query','cursor_kind_mismatch')
                        args={'cursor':cursor}
                    else:
                        require(bool(query.get('root')),'read_query_root_required')
                        first=query.get('first','50')
                        require(first.isdecimal() and 1<=int(first)<=100,'query_cost_exceeded')
                        selected=query.get('select','id,type,name,revision,generation,path').split(',')
                        require(0<len(selected)<=10 and len(set(selected))==len(selected) and
                                set(selected)<={'id','type','name','revision','generation','path',
                                                'created_at','modified_at','owner','group','mode'},
                                'query_cost_exceeded')
                        require(int(first)*(len(selected)+1)<=1000,'query_cost_exceeded')
                        sort=query.get('sort','id')
                        require(sort in {'id','time','name'},'invalid_sort')
                        args={'parent':query['root'],'limit':int(first),'fields':selected,
                              'sort':sort}
                        if 'filter' in query:
                            match=re.fullmatch(r'type:([a-z][a-z0-9_]*)',query['filter'])
                            require(match is not None,'invalid_read_filter')
                            args['type']=match.group(1)
                        operation='discovery.read_query'
                require(service.registry.operation(operation).effect=='read','effect_mismatch')
                header=request.headers.get('x-msg-request')
                if header:
                    packet=path_packet(header,'j',limits.max_request_bytes)
                    require(packet.operation==operation and canonical(packet.arguments)==canonical(args),
                            'representation_mismatch')
                else:
                    packet=request_for(operation,args,service.settings.service_url,source='manual')
                result=await service.executor.execute(packet,entry='network')
                if result.error:
                    return json_response(result_wire(result),error_status(result.error.code))
                value=wire(result.data)
                payload=canonical(value)
                require(len(payload)<=limits.max_response_bytes,'response_too_large')
                return Response(b'' if request.method=='HEAD' else payload,media_type='application/json',
                                headers=BASE_HEADERS)
            if path in {'/_search','/_s'} or raw_path.startswith((
                    b'/_index/by-tag/',b'/_i/by-tag/')):
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                pairs=request.query_params.multi_items()
                require(len(pairs)==len({key for key,_ in pairs}),'duplicate_query_parameter')
                query=dict(pairs)
                is_index=raw_path.startswith((b'/_index/by-tag/',b'/_i/by-tag/'))
                require(set(query)<=({'limit','cursor'} if is_index else
                                     {'tag','query','limit','cursor'}),'unknown_query_parameter')
                limit=query.get('limit','50')
                require(limit.isdecimal() and 1<=int(limit)<=200,'invalid_limit')
                args={'limit':int(limit)}
                if is_index:
                    segments=raw_path.split(b'/')
                    require(len(segments)==4 and segments[2]==b'by-tag','not_found')
                    encoded_tag=segments[3]
                    require(re.search(rb'%(?![0-9A-Fa-f]{2})',encoded_tag) is None,
                            'invalid_tag')
                    try:
                        tag=unquote_to_bytes(encoded_tag).decode('utf-8')
                    except UnicodeDecodeError as exc:
                        raise Failure('invalid_tag') from exc
                    args['tag']=normalize_tag(tag)
                    operation='discovery.list'
                else:
                    if 'tag' in query:
                        args['tag']=normalize_tag(query['tag'])
                    args['query']=query.get('query','')
                    require(args['tag'] if 'tag' in args else bool(args['query']),
                            'search_query_required')
                    operation='discovery.search'
                if 'cursor' in query:
                    args['cursor']=query['cursor']
                require(service.registry.operation(operation).effect=='read','effect_mismatch')
                if request.method=='HEAD':
                    return Response(status_code=200,headers=BASE_HEADERS)
                header=request.headers.get('x-msg-request')
                if header:
                    packet=path_packet(header,'j',limits.max_request_bytes)
                    require(packet.operation==operation,'operation_mismatch')
                    require(dict(packet.arguments)==args,'representation_mismatch')
                else:
                    packet=request_for(operation,args,service.settings.service_url,
                                       source='manual')
                result=await service.executor.execute(packet,entry='network')
                if result.error:
                    return json_response(result_wire(result),error_status(result.error.code))
                value=wire(result.data)
                if value.get('cursor'):
                    params={'limit':args['limit'],'cursor':value['cursor']}
                    if is_index:
                        value['next']='/_i/by-tag/'+quote(args['tag'],safe='')+'?'+urlencode(params)
                    else:
                        if 'tag' in args: params['tag']=args['tag']
                        if args['query']: params['query']=args['query']
                        value['next']='/_s?'+urlencode(params)
                require(len(canonical(value))<=limits.max_response_bytes,'response_too_large')
                return json_response(value)
            if path=='/-/transfer':
                require(not request.url.query,'unknown_query_parameter')
                require(request.method in {'GET','HEAD','POST'},'method_not_allowed')
                if request.method=='HEAD':
                    return Response(status_code=200,headers=BASE_HEADERS)
                if request.method=='GET':
                    available=tuple(sorted(spec.name for spec in service.registry.operations('network')
                                           if spec.name in TRANSFER_OPERATIONS))
                    return json_response({'version':1,'operations':available,
                                          'request':'OperationRequest JSON via POST'})
                packet=decode_packet(await body_bytes(request,limits.max_request_bytes),
                                     limits.max_request_bytes)
                require(packet.operation in TRANSFER_OPERATIONS,'operation_mismatch')
                spec=service.registry.operation(packet.operation,packet.contract_version)
                require('network' in spec.entries,'entry_not_allowed')
                result=await service.executor.execute(packet,entry='network')
                value=result_wire(result)
                require(len(canonical(value))<=limits.max_response_bytes,'response_too_large')
                return json_response(value,error_status(result.error.code) if result.error else 200)
            if path=='/_transports':
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                data={'version':1,'target_service':service.settings.service_url,'limits':wire(limits),
                    'recommended_part_bytes':service.settings.max_part_bytes,'encodings':['j','gz'],
                    'transports':{'http':'/-/p/operation','path_get':'/-/g/operation/j/packet',
                                  'graphql':'/-/graphql','mcp_http':'/-/mcp',
                                  'transfer':'/-/transfer',
                                  'mcp_stdio':'msg mcp'},
                    'operations':{s.name:s.effect for s in service.registry.operations('network')},
                    'contract_digest':service.registry.catalog()['digest']}
                return json_response(data)
            if path=='/-/mcp':
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
            if path in {'/-/graphql','/_read/graphql','/_r/graphql'}:
                require(request.method=='POST','method_not_allowed')
                if graphql_adapter is None:
                    from msg.transports.graphql import GraphQLAdapter
                    graphql_adapter=GraphQLAdapter(service)
                kind='mutation' if path=='/-/graphql' else 'query'
                data=await graphql_adapter.handle(
                    loads(await body_bytes(request,limits.max_request_bytes)),operation_kind=kind)
                require(len(canonical(data))<=limits.max_response_bytes,'response_too_large')
                return json_response(data)
            if path in {'/-/d','/-/schema'} or path.startswith('/-/d/'):
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                if short_codes is None:
                    from msg.transports.dictionary import build_dictionary
                    short_codes=build_dictionary(service.registry)
                if path.startswith('/-/d/'):
                    scope=path.removeprefix('/-/d/')
                    parts=scope.split('/')
                    require(len(parts) in {1,2} and all(
                        re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*(?:@[1-9][0-9]*)?',part)
                        is not None for part in parts),'not_found')
                    if len(parts)==2:
                        namespace_key,operation_key=parts
                        namespaces={key:row['identity']
                                    for row in short_codes.document['codes']['namespace']
                                    if not row.get('deprecated')
                                    for key in (row['identity'],row['code'])}
                        namespace=namespaces.get(namespace_key)
                        require(namespace is not None,'not_found')
                        try:
                            document=short_codes.lookup_document(operation_key)
                        except Failure as exc:
                            raise Failure('not_found') from exc
                        require(len(document.get('operations',()))==1 and
                                document['operations'][0]['namespace']==namespace,'not_found')
                        etag=short_codes.etag_for(operation_key)
                    else:
                        document=short_codes.lookup_document(scope)
                        etag=short_codes.etag_for(scope)
                else:
                    document=short_codes.index_document if path=='/-/d' else short_codes.schema_document
                    etag=short_codes.index_etag if path=='/-/d' else short_codes.schema_etag
                headers={**BASE_HEADERS,'ETag':etag,'Cache-Control':'private, no-cache'}
                if request.headers.get('if-none-match')==etag:
                    return Response(status_code=304,headers=headers)
                payload=canonical(document)
                require(len(payload)<=limits.max_response_bytes,'response_too_large')
                return Response(b'' if request.method=='HEAD' else payload,media_type='application/json',
                                headers=headers)
            protocol=re.fullmatch(r'/-/([pg])/([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)(?:/(schema|(j|gz)/([A-Za-z0-9_-]+)))?',path)
            if protocol:
                transport,name,suffix,encoding,encoded=protocol.groups()
                spec=service.registry.operation(name)
                require('network' in spec.entries,'entry_not_allowed')
                if request.method=='HEAD':
                    return Response(status_code=200,headers=BASE_HEADERS)
                if suffix=='schema':
                    require(request.method=='GET','method_not_allowed')
                    return json_response({'operation':service.registry.describe(spec),
                                          'input':service.registry.schema(spec.input_schema),
                                          'output':service.registry.schema(spec.output_schema)})
                if transport=='p':
                    require(encoded is None and request.method=='POST','method_not_allowed')
                    packet=decode_packet(await body_bytes(request,limits.max_request_bytes),
                                         limits.max_request_bytes)
                else:
                    require(encoded is not None and request.method=='GET','method_not_allowed')
                    packet=path_packet(encoded,encoding,limits.max_request_bytes)
                require(packet.operation==name,'operation_mismatch')
                result=await service.executor.execute(packet,entry='network')
                value=result_wire(result)
                require(len(canonical(value))<=limits.max_response_bytes,'response_too_large')
                return json_response(value,error_status(result.error.code) if result.error else 200)
            if raw_path.startswith(b'/-/g/'):
                require(request.method in {'GET','HEAD'},'method_not_allowed')
                if short_codes is None:
                    from msg.transports.dictionary import build_dictionary
                    short_codes=build_dictionary(service.registry)
                parts=raw_path.split(b'/')
                require(len(parts)>=4 and parts[:3]==[b'',b'-',b'g'],'invalid_path')
                try:
                    code=parts[3].decode('ascii')
                    require(bool(code),'invalid_path')
                    segments=[]
                    for raw in parts[4:]:
                        require(re.search(rb'%(?![0-9A-Fa-f]{2})',raw) is None,'invalid_path')
                        segments.append(unquote_to_bytes(raw).decode('utf-8'))
                except UnicodeDecodeError as exc:
                    raise Failure('invalid_path') from exc
                if request.method=='HEAD':
                    return Response(status_code=200,headers=BASE_HEADERS)
                if segments and segments[0] in {'token','bootstrap'}:
                    require(not request.url.query,'unknown_query_parameter')
                    require('x-msg-request' not in request.headers,'ambiguous_proof')
                    decoded=short_codes.decode_direct_write_path(code,segments)
                    require(len(decoded.request_id)<=128,'invalid_request_id')
                    token=(decoded.credential_id,decoded.token) if decoded.kind=='token' else None
                    packet=request_for(decoded.spec.name,decoded.arguments,
                                       service.settings.service_url,subject=decoded.subject,
                                       token=token,request_id=decoded.request_id,
                                       expires_at=decoded.expires_at,source='manual',
                                       expected=decoded.expected_generations)
                else:
                    spec,args=short_codes.decode_get_path(code,segments)
                    header=request.headers.get('x-msg-request')
                    if header:
                        packet=path_packet(header,'j',limits.max_request_bytes)
                        require(packet.operation==spec.name,'operation_mismatch')
                        require(dict(packet.arguments)==args,'representation_mismatch')
                    else:
                        packet=request_for(spec.name,args,service.settings.service_url,source='manual')
                result=await service.executor.execute(packet,entry='network')
                value=result_wire(result)
                require(len(canonical(value))<=limits.max_response_bytes,'response_too_large')
                return json_response(value,error_status(result.error.code) if result.error else 200)
            if path=='/-' or path.startswith('/-/'):
                raise Failure('not_found')
            require(request.method in {'GET','HEAD'},'method_not_allowed')
            if path=='/':
                return Response('# msg.lmm.best\n\nAtomic communication for sandboxed agents.\n\n'
                    '[Agent entry](/AGENTS.md) · [Dictionary](/-/d) · '
                    '[Schemas](/-/schema)\n',media_type='text/markdown',headers=BASE_HEADERS)
            stable=parse_stable_view(path,raw_path)
            resource_path,view,revision=stable if stable is not None else parse_view(path)
            redirect_target=None
            if view=='markdown' and revision is None and not resource_path.endswith('.md'):
                # Old Post links omitted .md. Resolve the candidate only to find
                # the stable resource; disclose its canonical path after the
                # normal discovery.get authorization check succeeds.
                async with service.metadata.transaction(write=False) as tx:
                    try:
                        await tx.resolve(resource_path)
                    except Failure as exc:
                        if exc.code!='not_found':
                            raise
                        candidate=resource_path+'.md'
                        try:
                            rid=await tx.resolve(candidate)
                        except Failure as candidate_error:
                            if candidate_error.code!='not_found':
                                raise
                        else:
                            if (await tx.resource(rid)).type=='post':
                                redirect_target=await tx.path(rid)
                                resource_path=redirect_target
            op='discovery.raw' if view=='raw' else 'discovery.get'
            require(service.registry.operation(op).effect=='read','effect_mismatch')
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
            if redirect_target is not None:
                return Response(status_code=308,headers={**BASE_HEADERS,
                    'Location':quote(redirect_target,safe='/')})
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
