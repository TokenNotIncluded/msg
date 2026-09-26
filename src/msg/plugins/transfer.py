"""All network adapters expose these same six transfer operations."""
from __future__ import annotations
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from msg.core.codec import b64,unb64,wire,decode,canonical,loads
from msg.core.errors import require
from msg.core.models import ResourceRef,HandlerOutput
from msg.core.transfer import TransferService
from msg.plugins.common import registration,resolve,create_resource,check_access
from msg.plugins.schemas import obj,INTEGER,STRING,IDENTIFIER,BYTES,REF

DIGEST={'type':'string','pattern':'^sha256:[0-9a-f]{64}$'}
SIZE={'type':'integer','minimum':0,'maximum':2**63-1}


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
    path_prefix=len('/!transfer.part_put/run/j/')
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

    finish()
