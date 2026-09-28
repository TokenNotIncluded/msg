"""One persistent transfer state machine, independently of the adapter."""
import os
import time
from datetime import timedelta

import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, decode, digest, loads, unb64, wire
from msg.core.models import TransferChunk
from msg.workers.maintenance import _collect, run_maintenance


@pytest.mark.asyncio
async def test_upload_out_of_order_conflicts_seal_and_download(installed):
    app,_=installed
    key,uid,cert=await register(app,'transfer-user')
    async def invoke(op,a):
        return await call(app,op,a,key=key,subject=uid,certs=(cert,))
    data=b'plain text\n'+bytes(range(256))*7
    opened=await invoke('transfer.open',{'direction':'upload','size':len(data),'digest':digest(data),
                                       'requested_part_bytes':512,'media_type':'application/octet-stream'})
    assert opened.status=='ok',wire(opened)
    tid=opened.data['transfer_id']
    size=opened.data['part_bytes']
    assert 0<size<=512
    ranges=list(range(0,len(data),size))
    for offset in reversed(ranges):
        block=data[offset:offset+size]
        args={'transfer_id':tid,'offset':offset,'data':b64(block),'digest':digest(block)}
        result=await invoke('transfer.part_put',args)
        assert result.status=='ok',wire(result)
        duplicate=await invoke('transfer.part_put',args)
        assert duplicate.status=='ok',wire(duplicate)
    corrupt=await invoke('transfer.part_put',{'transfer_id':tid,'offset':0,'data':b64(b'bad'),'digest':digest(b'bad')})
    assert corrupt.error.code=='chunk_conflict',wire(corrupt)
    wrong=await invoke('transfer.seal',{'transfer_id':tid,'final_size':len(data),'final_digest':digest(b'wrong')})
    assert wrong.status=='error',wire(wrong)
    status=await invoke('transfer.status',{'transfer_id':tid})
    assert status.data['missing']==(),wire(status)
    sealed=await invoke('transfer.seal',{'transfer_id':tid,'final_size':len(data),'final_digest':digest(data)})
    assert sealed.status=='ok',wire(sealed)
    again=await invoke('transfer.seal',{'transfer_id':tid,'final_size':len(data),'final_digest':digest(data)})
    assert again.resources==sealed.resources
    download=await invoke('transfer.open',{'direction':'download','target':wire(sealed.resources[0]),'requested_part_bytes':93})
    assert download.status=='ok',wire(download)
    restored=bytearray()
    dtid=download.data['transfer_id']
    for offset in range(0,len(data),93):
        part=await invoke('transfer.part_get',{'transfer_id':dtid,'offset':offset,'length':min(93,len(data)-offset)})
        assert part.status=='ok',wire(part)
        block=unb64(part.data['data'])
        assert digest(block)==part.data['chunk']['content']['digest']
        restored.extend(block)
    assert bytes(restored)==data
    # Sealing is not posting. The private output is a file resource.
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(sealed.resources[0].id)).type=='file'
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0]==0


@pytest.mark.asyncio
async def test_transfer_owner_bounds_missing_ranges_and_cancel(installed):
    app,_=installed
    key,uid,cert=await register(app,'first-agent')
    other,other_id,other_cert=await register(app,'second-agent')
    async def invoke(op,a):
        return await call(app,op,a,key=key,subject=uid,certs=(cert,))
    opened=await invoke('transfer.open',{'direction':'upload','size':8,'digest':digest(b'12345678')})
    assert opened.status=='ok',wire(opened)
    tid=opened.data['transfer_id']
    await invoke('transfer.part_put',{'transfer_id':tid,'offset':4,'data':b64(b'5678'),'digest':digest(b'5678')})
    status=await invoke('transfer.status',{'transfer_id':tid})
    assert status.data['missing']==((0,4),),wire(status)
    denied=await call(app,'transfer.status',{'transfer_id':tid},key=other,subject=other_id,certs=(other_cert,))
    assert denied.error.code=='transfer_owner_required',wire(denied)
    failed=await invoke('transfer.seal',{'transfer_id':tid,'final_size':8,'final_digest':digest(b'12345678')})
    assert failed.error.code=='transfer_incomplete',wire(failed)
    cancelled=await invoke('transfer.cancel',{'transfer_id':tid})
    assert cancelled.status=='ok',wire(cancelled)
    retry=await invoke('transfer.part_put',{'transfer_id':tid,'offset':0,'data':b64(b'1234'),'digest':digest(b'1234')})
    assert retry.error.code=='transfer_closed',wire(retry)
    tiny=await invoke('transfer.open',{'direction':'upload','max_path_bytes':100})
    assert tiny.error.code=='transport_limit_too_small',wire(tiny)


@pytest.mark.asyncio
async def test_expired_transfer_releases_chunk_pins_for_collection(installed):
    app,_=installed
    key,uid,cert=await register(app,'expiring-transfer')
    opened=await call(app,'transfer.open',{'direction':'upload','size':4},
                      key=key,subject=uid,certs=(cert,))
    tid=opened.data['transfer_id']
    data=b'abcd'
    await call(app,'transfer.part_put',{'transfer_id':tid,'offset':0,
        'data':b64(data),'digest':digest(data)},key=key,subject=uid,certs=(cert,))
    async with app.metadata.transaction(write=False) as tx:
        chunk=decode(TransferChunk,loads(tx.one(
            'SELECT body FROM chunks WHERE transfer_id=? AND offset=0',(tid,))[0]))
    assert await app.contents.pinned(chunk.content,tid+':0')

    app.clock=lambda: NOW+timedelta(seconds=app.settings.transfer_ttl+1)
    result=await run_maintenance(app,'cleanup_expired',scheduled=True)
    assert result['expired_transfers']==1
    assert not await app.contents.pinned(chunk.content,tid+':0')
    index=app.contents.index/chunk.content.digest[7:]
    old=time.time()-7200
    os.utime(index,(old,old))
    async with app.metadata.transaction(write=True) as tx:
        collected=await _collect(app,tx,grace_seconds=0)
        assert tx.one('SELECT COUNT(*) FROM chunks WHERE transfer_id=?',(tid,))[0]==0
    assert collected['unreferenced_contents_removed']==1
    assert not index.exists()
