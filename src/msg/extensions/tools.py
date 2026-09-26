"""Certificate-scoped tool discovery and durable explicit invocation."""
from __future__ import annotations
from dataclasses import replace
from msg.constants import TOOLS_SPACE,ROOT_SUBJECT
from msg.core.codec import canonical,decode,digest,wire,loads
from msg.core.errors import Failure,require
from msg.core.models import ToolSpec,NetworkPolicy,ResourceRef,EffectJob,HandlerOutput
from msg.plugins.common import resolve,check_access,new_id,operation_id
from msg.plugins.communication import event_id
from msg.plugins.schemas import obj,STRING,IDENTIFIER,REF,BYTES
from msg.security.network import intersect_policy,validate_url,normalized_host,validate_addresses
from msg.security.policy import grant_covers

DNS_INPUT=obj({'name':{'type':'string','minLength':1,'maxLength':253},'type':{'enum':['A','AAAA','TXT','MX','NS','CNAME','SRV']}},('name','type'))
CURL_INPUT=obj({'url':{'type':'string','minLength':1,'maxLength':8192},
    'method':{'enum':['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS']},
    'headers':{'type':'object','maxProperties':30,'additionalProperties':{'type':'string','maxLength':4096}},
    'body':BYTES,'body_ref':REF},('url',))
TOOL_OUTPUT={'type':'object'}


def descriptor(name):
    return {'tool_id':'tool_'+name,'version':1,'executor_key':name,'operation':'tool.invoke',
        'required_capabilities':['tool.use'],'input_schema':{'id':'schema:tool.'+name+':input'},
        'output_schema':{'id':'schema:tool.'+name+':output'},
        'network':{'schemes':['http','https'],'hosts':[],'ports':[80,443],
            'methods':['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'],
            'allow_private':True,'timeout_ms':30000,'max_response_bytes':16777216,'max_redirects':5},
        'default_targets':'public','description':'Structured DNS query' if name=='dns' else 'Bounded HTTP(S) request; not a shell'}


async def read_tool(app,tx,rid,revision=None):
    resource=await tx.resource(rid)
    require(resource.type=='tool' and resource.parent==TOOLS_SPACE,'not_a_tool')
    rev=await tx.revision(ResourceRef(id=rid,revision=revision))
    value=loads(await app.contents.read_bytes(rev.content,limit=65536))
    require(value['executor_key'] in {'dns','curl'} and value['tool_id']==rid,'untrusted_tool_executor')
    return ToolSpec(resource=ResourceRef(id=rid,revision=rev.id),operation='tool.invoke',
        input_schema=decode(ResourceRef,value['input_schema']),output_schema=decode(ResourceRef,value['output_schema']),
        executor_key=value['executor_key'],network=decode(NetworkPolicy,value['network']))


async def tool_policies(app,principal,tool,args,tx):
    operation='tool.invoke@1'
    grants=await app.authorizer.grants(principal,tx)
    certs=[g for g in grants if await grant_covers(g,'tool.use',operation,tool.resource.id,tx)]
    ceilings=[g for g in principal.ceiling if await grant_covers(g,'tool.use',operation,tool.resource.id,tx)]
    private=[g for g in grants if await grant_covers(g,'tool.net.private',operation,tool.resource.id,tx)]
    private_ceil=[g for g in principal.ceiling if await grant_covers(g,'tool.net.private',operation,tool.resource.id,tx)]
    require(certs and ceilings,'tool_certificate_required')
    deployment={'timeout_ms':app.settings.tool_timeout_ms,'max_response_bytes':app.settings.tool_max_response_bytes,
                'methods':app.settings.tool_methods,'ports':app.settings.tool_ports}
    base=intersect_policy(tool.network,deployment)
    candidates=[]
    for grant in certs:
        for ceiling in ceilings:
            try:
                value=intersect_policy(intersect_policy(base,grant.constraints),ceiling.constraints)
                candidates.append(replace(value,allow_private=False))
                if base.allow_private:
                    for private_grant in private:
                        for private_ceiling in private_ceil:
                            candidates.append(replace(intersect_policy(intersect_policy(value,private_grant.constraints),
                                private_ceiling.constraints),allow_private=True))
            except Failure as exc:
                if exc.code!='network_policy_empty':raise
    allowed=[]
    for policy in candidates:
        try:
            if tool.executor_key=='curl':
                parsed=validate_url(args['url'],args.get('method','GET'),policy)
                import ipaddress
                try:address=ipaddress.ip_address(parsed.hostname)
                except ValueError:address=None
                if address is not None:validate_addresses([str(address)],policy)
            else:
                host=normalized_host(args['name'])
                require(not policy.hosts or host in policy.hosts,'network_host_forbidden')
            allowed.append(policy)
        except Failure:
            continue
    require(allowed,'network_policy_denied')
    # Alternatives preserve unions of grants; the child checks every resolution
    # and redirect against the complete conjunction selected for that target.
    return tuple(allowed)


def register(app,op):
    for name,schema in (('dns',DNS_INPUT),('curl',CURL_INPUT)):
        app.registry.add_schema(ResourceRef(id='schema:tool.'+name+':input'),schema)
        app.registry.add_schema(ResourceRef(id='schema:tool.'+name+':output'),TOOL_OUTPUT)

    @op('tool.invoke',obj({'id':IDENTIFIER,'revision':IDENTIFIER,'arguments':{'type':'object'}},('id','arguments')),effect='external')
    async def invoke(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'tool_use')
        tool=await read_tool(app,tx,rid,request.arguments.get('revision'))
        args=request.arguments['arguments']
        app.registry.validate(tool.input_schema,args)
        policies=await tool_policies(app,ctx.principal,tool,args,tx)
        if args.get('body_ref') is not None:
            ref=decode(ResourceRef,args['body_ref'])
            require(ref.revision is not None,'source_revision_required')
            await check_access(app,ctx,request,tx,ref.id,'read')
            await tx.revision(ref)
        require(not ('body' in args and 'body_ref' in args),'ambiguous_request_body')
        eid=event_id(request,ctx.principal.subject)
        job=EffectJob(id=new_id('job'),event_id=eid,kind='tool',dedupe_key='tool:'+eid,principal=ctx.principal,
            operation=request.operation,arguments={'tool':wire(tool.resource),'input':wire(args),
                'policies':wire(policies),'request_id':request.request_id},state='pending',attempts=0,
                next_attempt_at=ctx.now,lease_until=None)
        await tx.enqueue(job)
        return HandlerOutput(resources=(tool.resource,),data={'job_id':job.id,'state':'pending','tool_id':rid,
            'input_digest':digest(args),'target_digest':digest(args.get('url',args.get('name','')))})

    @op('job.get',obj({'id':IDENTIFIER},('id',)),effect='read')
    async def job_get(ctx,request,tx):
        job=await tx.job(request.arguments['id'])
        require(job.principal.subject==ctx.principal.subject,'job_owner_required')
        if job.kind=='tool':
            await check_access(app,ctx,request,tx,job.arguments['tool']['id'],'read')
        else:
            await app.authorizer._ceiling(ctx.principal,operation_id(request),ctx.principal.subject,tx)
        data={'id':job.id,'state':job.state,'attempts':job.attempts,'event_id':job.event_id}
        if job.result:
            await check_access(app,ctx,request,tx,job.result.id,'read')
            data['output']=wire(job.result)
        status=tx.setting('job_status:'+job.id)
        if status:data['result']=status
        return HandlerOutput(data=data,output=job.result)
