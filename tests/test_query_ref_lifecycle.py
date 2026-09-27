"""Query descriptor cleanup runs only in maintenance after effective references expire."""
from datetime import timedelta

import httpx
import pytest

from msg.core.codec import b64, canonical, digest
from msg.core.requests import request_for
from msg.transports.http import create_app
from msg.workers.maintenance import run_maintenance
from test_service import NOW, call, register


@pytest.mark.asyncio
async def test_query_source_is_retained_for_valid_ref_then_purged_by_maintenance(installed):
    app, _ = installed
    key,user,_=await register(app,'query-retention')
    descriptor=canonical({'version':1,'kind':'read',
                          'arguments':{'parent':'/main','limit':1}})
    opened=await call(app,'transfer.open',{'direction':'upload','size':len(descriptor),
        'digest':digest(descriptor),'media_type':'application/vnd.msg.read-query+json'},
        key=key,subject=user)
    tid=opened.data['transfer_id']
    await call(app,'transfer.part_put',{'transfer_id':tid,'offset':0,
        'data':b64(descriptor),'digest':digest(descriptor)},key=key,subject=user)
    sealed=await call(app,'transfer.seal',{'transfer_id':tid,
        'final_size':len(descriptor),'final_digest':digest(descriptor)},key=key,subject=user)
    ref=sealed.resources[0]
    issued=await call(app,'transfer.query_seal',{'transfer_id':tid},key=key,subject=user)
    assert issued.status=='ok'
    async with app.metadata.transaction(write=False) as tx:
        marker=tx.setting('query_ref_source:'+ref.id)
        assert marker['transfer_id']==tid
        assert (await tx.resource(ref.id)).state=='active'
    # A second Resource keeps the same immutable bytes live after source cleanup.
    copied=await call(app,'content.file_put',{'parent':'/@query-retention/files',
        'name':'retained-copy','source':{'id':ref.id,'revision':ref.revision}},
        key=key,subject=user)
    assert copied.status=='ok'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        await http.get('/_r/q/'+issued.data['query_ref'])
        app.clock=lambda: NOW+timedelta(minutes=16)
        app.executor.clock=app.clock
        app.authenticator.clock=app.clock
        expired_packet=request_for('transfer.query_get',
            {'query_ref':issued.data['query_ref']},app.settings.service_url,
            signer=key,subject=user,
            expires_at=NOW+timedelta(minutes=17))
        expired=await http.get('/_r/q/'+issued.data['query_ref']+'/p/'+
                               b64(canonical(expired_packet)))
        assert expired.status_code==400
        assert expired.json()['error']['code']=='query_ref_expired'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(ref.id)).state=='active'
    app.clock=lambda: NOW+timedelta(seconds=app.settings.transfer_ttl-3600)
    app.executor.clock=app.clock
    app.authenticator.clock=app.clock
    download_packet=request_for('transfer.open',
        {'direction':'download','target':{'id':ref.id,'revision':ref.revision}},
        app.settings.service_url,signer=key,subject=user,
        expires_at=app.clock()+timedelta(seconds=120))
    download=await app.executor.execute(download_packet)
    assert download.status=='ok'
    app.clock=lambda: NOW+timedelta(seconds=app.settings.transfer_ttl+3601)
    held=await run_maintenance(app,'cleanup_expired',scheduled=True)
    assert held['expired_query_sources']==0
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(ref.id)).state=='active'
    app.clock=lambda: NOW+timedelta(seconds=2*app.settings.transfer_ttl+3601)
    result=await run_maintenance(app,'cleanup_expired',scheduled=True)
    assert result['expired_query_sources']==1
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(ref.id)).state=='purged'
        copy=await tx.revision(copied.resources[0])
    assert await app.contents.read_bytes(copy.content)==descriptor
