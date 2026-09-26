from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import pytest

from msg.core.codec import wire,canonical,digest
from msg.core.errors import Failure
from msg.core.models import Scope,NetworkPolicy,ResourceRef
from msg.security.network import validate_url,validate_addresses,intersect_policy
from msg.workers.effects import EffectWorker,ToolResult
from test_service import register,call,NOW
from test_authorization import approve,scoped


def policy(**kw):
    return NetworkPolicy(schemes=frozenset({'http','https'}),hosts=(),ports=frozenset({80,443}),
        methods=frozenset({'GET','HEAD'}),allow_private=kw.get('allow_private',False),timeout_ms=1000,
        max_response_bytes=10000,max_redirects=3)


@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:pass@example.org','http://example.org\\@127.0.0.1',
    'http://example.org:22/','unix:///var/run/socket','http://example.org/\nX:test'])
def test_network_tool_rejects_non_http_or_ambiguous_targets(url):
    with pytest.raises(Failure):validate_url(url,'GET',policy())


@pytest.mark.parametrize('address',['127.0.0.1','10.2.3.4','169.254.169.254','::1','::ffff:127.0.0.1','fe80::1','0.0.0.0','100.64.0.1'])
def test_public_tool_rejects_nonpublic_addresses(address):
    with pytest.raises(Failure,match='private_target_forbidden'):validate_addresses([address],policy())
    if address=='0.0.0.0':
        with pytest.raises(Failure,match='invalid_network_target'):validate_addresses([address],policy(allow_private=True))
    else:
        assert validate_addresses([address],policy(allow_private=True))==[address]


def test_tool_policy_intersection_does_not_widen_allowlists_or_limits():
    restricted=intersect_policy(policy(),{'hosts':['example.org'],'methods':['GET'],'timeout_ms':500})
    assert validate_url('https://example.org/path','GET',restricted).hostname=='example.org'
    assert restricted.timeout_ms==500
    with pytest.raises(Failure):validate_url('https://other.example','GET',restricted)
    with pytest.raises(Failure):validate_url('https://example.org','HEAD',restricted)
    with pytest.raises(Failure):intersect_policy(policy(),{'ports':[22]})


@pytest.mark.asyncio
async def test_only_scoped_tool_is_visible_jobs_are_deduped_and_output_transfers(installed,tmp_path):
    app,root=installed
    key,uid,base=await register(app,'tool-agent')
    denied=await call(app,'tool.invoke',{'id':'/_tools/dns','arguments':{'name':'example.org','type':'A'}},key=key,subject=uid)
    assert denied.error.code=='tool_certificate_required',wire(denied)
    cap=scoped(app,'tool.use','tool_dns',app.registry.capability('tool.use').operations)
    cert=await approve(app,root,uid,key,(cap,))
    tools=await call(app,'discovery.get',{'id':'/_tools'},key=key,subject=uid,certs=(cert.resource_id,))
    assert [t['name'] for t in tools.data['items']]==['dns']
    wrong=await call(app,'tool.invoke',{'id':'/_tools/curl','arguments':{'url':'https://example.org'}},key=key,subject=uid,certs=(cert.resource_id,))
    assert wrong.error.code=='tool_certificate_required'
    args={'id':'/_tools/dns','arguments':{'name':'example.org','type':'A'}}
    first=await call(app,'tool.invoke',args,key=key,subject=uid,certs=(cert.resource_id,),rid='tool-once')
    second=await call(app,'tool.invoke',args,key=key,subject=uid,certs=(cert.resource_id,),rid='tool-once')
    assert first.status=='accepted' and second.replayed,wire(first)
    assert first.data['job_id']==second.data['job_id']
    executions=[]
    async def runner(tool,args,policy,outdir):
        executions.append(tool.executor_key)
        path=outdir/'output.bin';path.write_bytes(canonical({'answers':['93.184.216.34']}))
        return ToolResult(path=path,media_type='application/json',metadata={'kind':'dns'})
    worker=EffectWorker(app,tool_runner=runner)
    assert await worker.run_once()
    assert not await worker.run_once()
    assert executions==['dns']
    state=await call(app,'job.get',{'id':first.data['job_id']},key=key,subject=uid,certs=(cert.resource_id,))
    assert state.data['state']=='done',wire(state)
    ref=state.data['output']
    download=await call(app,'transfer.open',{'direction':'download','target':ref},key=key,subject=uid,certs=(cert.resource_id,))
    assert download.status=='ok',wire(download)


@pytest.mark.asyncio
async def test_pending_job_rechecks_revocation_and_expired_lease_is_uncertain(installed,tmp_path):
    app,root=installed
    key,uid,base=await register(app,'worker-agent')
    cert=await approve(app,root,uid,key,(scoped(app,'tool.use','tool_dns',app.registry.capability('tool.use').operations),))
    result=await call(app,'tool.invoke',{'id':'/_tools/dns','arguments':{'name':'example.org','type':'A'}},key=key,subject=uid,certs=(cert.resource_id,))
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE certificates SET revoked=1 WHERE id=?',(cert.resource_id,),write=True)
    ran=[]
    async def runner(*args):ran.append(True)
    worker=EffectWorker(app,tool_runner=runner)
    await worker.run_once()
    assert not ran
    async with app.metadata.transaction(write=False) as tx:
        job=await tx.job(result.data['job_id'])
        assert job.state=='failed'
    cert2=await approve(app,root,uid,key,(scoped(app,'tool.use','tool_dns',app.registry.capability('tool.use').operations),))
    job_result=await call(app,'tool.invoke',{'id':'/_tools/dns','arguments':{'name':'example.org','type':'A'}},key=key,subject=uid,certs=(cert2.resource_id,))
    async with app.metadata.transaction(write=True) as tx:
        job=await tx.job(job_result.data['job_id'])
        await tx.save_job(replace(job,state='running',lease_until=NOW-timedelta(seconds=1)))
    await worker.run_once()
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(job.id)).state=='uncertain'
    assert not ran

@pytest.mark.asyncio
async def test_tool_external_success_then_revocation_records_uncertain(installed):
    app,root=installed
    key,uid,_=await register(app,'late-revocation')
    cert=await approve(app,root,uid,key,(scoped(app,'tool.use','tool_dns',app.registry.capability('tool.use').operations),))
    result=await call(app,'tool.invoke',{'id':'/_tools/dns','arguments':{'name':'example.org','type':'A'}},key=key,subject=uid,certs=(cert.resource_id,))
    async def runner(tool,args,policies,outdir):
        async with app.metadata.transaction(write=True) as tx:
            tx.execute('UPDATE certificates SET revoked=1 WHERE id=?',(cert.resource_id,),write=True)
        path=outdir/'result.bin';path.write_bytes(b'[]')
        return ToolResult(path=path,media_type='application/json',metadata={})
    await EffectWorker(app,tool_runner=runner).run_once()
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(result.data['job_id'])).state=='uncertain'
