"""All network adapters expose these same six transfer operations."""
from __future__ import annotations
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from msg.core.codec import b64,unb64,wire,decode,canonical,loads,parse_time,digest
from msg.core.errors import Failure,require
from msg.core.models import ResourceRef,HandlerOutput
from msg.core.tags import normalize_tag
from msg.core.transfer import TransferService
from msg.plugins.common import registration,resolve,create_resource,check_access
from msg.plugins.schemas import obj,INTEGER,STRING,IDENTIFIER,BYTES,REF

DIGEST={'type':'string','pattern':'^sha256:[0-9a-f]{64}$'}
SIZE={'type':'integer','minimum':0,'maximum':2**63-1}
QUERY_MEDIA='application/vnd.msg.read-query+json'


async def sealed_read_query(app,ctx,request,tx,transfer):
    require(transfer.subject_id==ctx.principal.subject,'query_ref_principal_mismatch')
    require(transfer.state=='sealed' and transfer.direction=='upload' and
            transfer.output is not None,'query_transfer_not_sealed')
    require(transfer.expires_at>ctx.now,'query_ref_expired')
    ref=transfer.output
    resource=await tx.resource(ref.id)
    require(resource.type=='file' and resource.owner==ctx.principal.subject and
            resource.state=='active','invalid_query_ref')
    await check_access(app,ctx,request,tx,ref.id,'read')
    revision=await tx.revision(ref)
    require(revision.content.media_type==QUERY_MEDIA and revision.content.size<=65536 and
            revision.content.digest==transfer.expected_digest,'query_ref_digest_mismatch')
    try:
        descriptor=loads(await app.contents.read_bytes(revision.content,limit=65536))
    except Failure as exc:
        raise Failure('invalid_query_ref') from exc
    require(type(descriptor) is dict and set(descriptor)=={'version','kind','arguments'} and
            descriptor['version']==1 and descriptor['kind']=='read' and
            type(descriptor['arguments']) is dict,'invalid_query_ref')
    args=descriptor['arguments']
    require(args.get('parent') is not None and 'cursor' not in args,'invalid_query_ref')
    try:
        app.registry.validate(app.registry.operation('discovery.read_query').input_schema,args)
    except Failure as exc:
        raise Failure('invalid_query_ref') from exc
    return ref,revision,args


def query_ref_principal(principal):
    return {'actor':principal.actor,'subject':principal.subject,
            'credential_id':principal.credential_id}


def negotiate(app,request):
    a=request.arguments
    limits=app.settings.server.limits
    request_bytes=min(limits.max_request_bytes,a.get('max_request_bytes',limits.max_request_bytes))
    response_bytes=min(limits.max_response_bytes,a.get('max_response_bytes',limits.max_response_bytes))
    path_bytes=min(limits.max_path_bytes,a.get('max_path_bytes',limits.max_path_bytes))
    # Calculate an envelope with maximal fixed-length IDs/offsets and the caller's
    # actual proof overhead. Two Base64 layers are needed for data inside a path.
    packet=wire(request)
    packet.update(operation='transfer.part_put',request_id='r'*64,payload_digest='sha256:'+'0'*64,
        arguments={'transfer_id':'tr_'+'0'*32,'offset':2**63-1,'data':'','digest':'sha256:'+'0'*64})
    overhead=len(canonical(packet))+48
    path_prefix=len('/-/g/transfer.part_put/j/')
    path_budget=(path_bytes-path_prefix)*3//4-overhead
    request_budget=request_bytes-overhead
    response_budget=response_bytes-1024
    chosen=min(app.settings.max_part_bytes,a.get('requested_part_bytes',app.settings.max_part_bytes),
               request_budget*3//4,path_budget*3//4,response_budget*3//4)
    require(chosen>=1,'transport_limit_too_small')
    return {'part_bytes':chosen,'max_request_bytes':request_bytes,'max_response_bytes':response_bytes,
            'max_path_bytes':path_bytes}


def install(app):
    op,finish=registration(app,'transfer',('identity','content'))

    def service():
        async def publish(ctx,tx,parent,blob):
            # An internal operation label is not a new or user-supplied principal.
            operation=SimpleNamespace(operation='transfer.seal',contract_version=1)
            resource=await create_resource(app,ctx,operation,tx,parent=parent,type='file',body=blob,
                media_type=blob.media_type,mode=0o600)
            return ResourceRef(id=resource.id,revision=resource.revision)
        return TransferService(app.metadata,app.contents,app.authorizer,app.clock,publish=publish,
                               ttl=app.settings.transfer_ttl,part_bytes=app.settings.max_part_bytes)

    @op('transfer.open',obj({'direction':{'enum':['upload','download']},'target':REF,'size':SIZE,'digest':DIGEST,
        'media_type':STRING,'requested_part_bytes':{'type':'integer','minimum':1},
        'max_request_bytes':{'type':'integer','minimum':1},'max_response_bytes':{'type':'integer','minimum':1},
        'max_path_bytes':{'type':'integer','minimum':1}},('direction',)))
    async def open_transfer(ctx,request,tx):
        a=request.arguments
        target=decode(ResourceRef,a['target']) if 'target' in a else None
        if target is not None and target.id.startswith('/'):
            target=replace(target,id=await resolve(tx,target.id))
        limits=negotiate(app,request)
        transfer=await service().open(ctx,a['direction'],target,a.get('size'),a.get('digest'),
            ctx.now+timedelta(seconds=app.settings.transfer_ttl),limits=limits,
            media_type=a.get('media_type','application/octet-stream'))
        return HandlerOutput(data={'transfer_id':transfer.id,'direction':transfer.direction,'state':transfer.state,
            'target':wire(transfer.target),'size':transfer.expected_size,'digest':transfer.expected_digest,
            'expires_at':wire(transfer.expires_at),**limits})

    @op('transfer.part_put',obj({'transfer_id':IDENTIFIER,'offset':SIZE,'data':BYTES,'digest':DIGEST},
                                ('transfer_id','offset','data','digest')))
    async def put(ctx,request,tx):
        a=request.arguments
        chunk=await service().put(ctx,a['transfer_id'],a['offset'],unb64(a['data'],limit=app.settings.max_part_bytes),a['digest'])
        return HandlerOutput(data={'chunk':wire(chunk)})

    @op('transfer.part_get',obj({'transfer_id':IDENTIFIER,'offset':SIZE,'length':SIZE},
                               ('transfer_id','offset','length')),effect='read')
    async def get(ctx,request,tx):
        a=request.arguments
        chunk,data=await service().get(ctx,a['transfer_id'],(a['offset'],a['offset']+a['length']))
        return HandlerOutput(data={'chunk':wire(chunk),'data':b64(data)})

    @op('transfer.status',obj({'transfer_id':IDENTIFIER,'cursor':STRING,'limit':{'type':'integer','minimum':1,'maximum':500}},
                              ('transfer_id',)),effect='read')
    async def status(ctx,request,tx):
        a=request.arguments
        page=await service().status(ctx,a['transfer_id'],a.get('cursor'),a.get('limit',50))
        transfer=await tx.transfer(a['transfer_id'])
        output={'transfer_id':transfer.id,'state':transfer.state,'direction':transfer.direction,
                'generation':transfer.generation,'size':transfer.expected_size,'digest':transfer.expected_digest,
                'missing':wire(page.items),'next_cursor':page.next_cursor,'output':wire(transfer.output)}
        if transfer.expected_size is None:
            after=int(a.get('cursor','0'))
            rows=tx.rows('SELECT offset,length FROM chunks WHERE transfer_id=? AND offset>=? ORDER BY offset LIMIT ?',
                        (transfer.id,after,a.get('limit',50)+1))
            selected=rows[:a.get('limit',50)]
            output['received']=[[row[0],row[0]+row[1]] for row in selected]
            output['next_cursor']=str(rows[-1][0]) if len(rows)>len(selected) else None
        return HandlerOutput(data=output)

    @op('transfer.seal',obj({'transfer_id':IDENTIFIER,'final_size':SIZE,'final_digest':DIGEST},
                           ('transfer_id','final_size','final_digest')))
    async def seal(ctx,request,tx):
        a=request.arguments
        ref=await service().seal(ctx,a['transfer_id'],a['final_size'],a['final_digest'])
        return HandlerOutput(resources=(ref,),data={'transfer_id':a['transfer_id'],'state':'sealed',
                                                  'size':a['final_size'],'digest':a['final_digest']},output=ref)

    @op('transfer.cancel',obj({'transfer_id':IDENTIFIER},('transfer_id',)))
    async def cancel(ctx,request,tx):
        await service().cancel(ctx,request.arguments['transfer_id'])
        return HandlerOutput(data={'transfer_id':request.arguments['transfer_id'],'state':'cancelled'})

    @op('transfer.query_seal',obj({'transfer_id':IDENTIFIER},('transfer_id',)))
    async def query_seal(ctx,request,tx):
        transfer=await tx.transfer(request.arguments['transfer_id'])
        ref,revision,_=await sealed_read_query(app,ctx,request,tx,transfer)
        expiry=min(transfer.expires_at,ctx.now+timedelta(minutes=15))
        source=await tx.resource(ref.id)
        marker_key='query_ref_source:'+ref.id
        previous=tx.setting(marker_key)
        retention=max(transfer.expires_at,expiry)+timedelta(hours=1)
        if previous is not None:
            retention=max(retention,parse_time(previous['retain_until']))
        tx.set_setting(marker_key,{'transfer_id':transfer.id,'subject':ctx.principal.subject,
            'revision':ref.revision,'digest':revision.content.digest,'parent':source.parent,
            'retain_until':wire(retention)})
        token=app.cursors.encode('query-ref',
            {'service':app.settings.service_url,'transfer_id':transfer.id,
             'output':wire(ref),'digest':revision.content.digest},
            {'principal':query_ref_principal(ctx.principal),'expires_at':wire(expiry)})
        return HandlerOutput(data={'query_ref':token,'expires_at':wire(expiry),
                                   'next':'/_r/q/'+token})

    @op('transfer.query_get',obj({'query_ref':STRING,'cursor':STRING},('query_ref',)),effect='read')
    async def query_get(ctx,request,tx):
        token=request.arguments['query_ref']
        signed=app.cursors.inspect(token)
        require(signed.get('kind')=='query-ref','invalid_query_ref')
        query,position=signed['query'],signed['position']
        require(query.get('service')==app.settings.service_url,'wrong_service')
        require(position.get('principal')==query_ref_principal(ctx.principal),
                'query_ref_principal_mismatch')
        require(ctx.now<parse_time(position['expires_at']),'query_ref_expired')
        transfer=await tx.transfer(query['transfer_id'])
        ref,revision,args=await sealed_read_query(app,ctx,request,tx,transfer)
        require(query.get('output')==wire(ref) and query.get('digest')==revision.content.digest,
                'query_ref_digest_mismatch')
        principal=query_ref_principal(ctx.principal)
        normalized=dict(args)
        if normalized.get('parent'):
            normalized['parent']=await resolve(tx,normalized['parent'])
        if normalized.get('tag'):
            normalized['tag']=normalize_tag(normalized['tag'])
        nested_args=args
        internal_page=None
        if request.arguments.get('cursor'):
            compact=app.cursors.inspect(request.arguments['cursor'])
            require(compact.get('kind')=='query-ref-page' and
                    compact.get('query',{}).get('query_ref_digest')==digest(token),
                    'invalid_cursor')
            page=compact['position']
            require(page.get('principal')==principal,'query_ref_principal_mismatch')
            require(ctx.now<parse_time(page['expires_at']),'cursor_expired')
            require(page.get('query_digest')==digest(normalized),'cursor_query_mismatch')
            internal_page={'arguments':normalized,'last':page['last'],
                           'snapshot':page['snapshot']}
        # Reuse the installed read handler and its current per-object Authorizer.
        # Query bytes can select only this registered read operation.
        nested=SimpleNamespace(operation='discovery.read_query',contract_version=1,
                               arguments=nested_args,internal_page_state=internal_page)
        output=await app.registry.operation('discovery.read_query').handler(ctx,nested,tx)
        data=dict(output.data)
        if data.get('cursor'):
            # This oversized cursor was generated in this call by the trusted
            # read handler. Never expose it or raise the public 8 KiB limit.
            encoded=data['cursor'].split('.',1)[0]
            generated=loads(unb64(encoded,limit=131072))
            require(generated.get('kind')=='read-page','invalid_cursor')
            page_query,page=generated['query'],generated['position']
            require(page_query=={'operation':'discovery.read_query','arguments':normalized},
                    'cursor_query_mismatch')
            expiry=min(parse_time(position['expires_at']),parse_time(page['expires_at']))
            short=app.cursors.encode('query-ref-page',{'query_ref_digest':digest(token)},
                {'last':page['last'],'snapshot':page['snapshot'],'principal':principal,
                 'query_digest':digest(normalized),'expires_at':wire(expiry)})
            data.update(cursor=short,next=f'/_r/q/{token}/c/{short}',next_requires_auth=True)
        return replace(output,data=data)

    finish()
