"""Durable SyncCursor advancement requires an explicit signed ACK."""
from datetime import timedelta

import httpx
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app
from test_service import NOW, call, register


def sync_headers(app,key,subject,cursor):
    packet=request_for('communication.sync',{'cursor':cursor},app.settings.service_url,
                       signer=key,subject=subject,expires_at=NOW+timedelta(seconds=60))
    return {'x-msg-request':b64(canonical(packet))}


async def read_sync(http,app,key,subject,cursor):
    return await http.get('/_r/s/'+cursor,headers=sync_headers(app,key,subject,cursor))


@pytest.mark.asyncio
async def test_checkpoint_expands_seen_beyond_64_and_requires_ack(installed):
    app,_=installed
    key,subject,_=await register(app,'checkpoint-many')
    other_key,other_subject,_=await register(app,'checkpoint-other')
    for index in range(70):
        result=await call(app,'content.post_create',{'parent':'/main','body':f'post {index}'},
                          key=key,subject=subject)
        assert result.status=='ok',wire(result)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        packet=request_for('communication.sync',{},app.settings.service_url,
                           signer=key,subject=subject,expires_at=NOW+timedelta(seconds=60))
        start=await http.get('/_r/s/start',headers={'x-msg-request':b64(canonical(packet))})
        assert start.status_code==200,start.text
        old=start.json()['sync_cursor']
        opened=await call(app,'communication.sync_checkpoint_open',{'cursor':old},
                          key=key,subject=subject,rid='checkpoint-open')
        assert opened.status=='ok',wire(opened)
        replay=await call(app,'communication.sync_checkpoint_open',{'cursor':old},
                          key=key,subject=subject,rid='checkpoint-open')
        assert replay.status=='ok' and replay.replayed
        cursor=opened.data['sync_cursor']
        forbidden=await read_sync(http,app,other_key,other_subject,cursor)
        assert forbidden.status_code==403
        assert forbidden.json()['error']['code']=='cursor_principal_mismatch'
        page=await read_sync(http,app,key,subject,cursor)
        again=await read_sync(http,app,key,subject,cursor)
        assert page.status_code==again.status_code==200
        assert page.json()==again.json()
        assert page.json()['items']
        async with app.metadata.transaction(write=False) as tx:
            version=tx.one('SELECT version FROM sync_checkpoints WHERE subject=?',(subject,))[0]
        assert version==0
        ack=page.json()['checkpoint_ack']
        committed=await call(app,'communication.sync_checkpoint_ack',{'ack':ack},
                             key=key,subject=subject,rid='checkpoint-ack')
        assert committed.status=='ok',wire(committed)
        retry=await call(app,'communication.sync_checkpoint_ack',{'ack':ack},
                         key=key,subject=subject,rid='checkpoint-ack')
        assert retry.status=='ok' and retry.replayed
        conflict=await call(app,'communication.sync_checkpoint_ack',{'ack':ack},
                            key=key,subject=subject,rid='checkpoint-concurrent')
        assert conflict.error.code=='checkpoint_conflict'
        stale_get=await read_sync(http,app,key,subject,cursor)
        assert stale_get.status_code==400
        assert stale_get.json()['error']['code']=='checkpoint_conflict'
        second=await read_sync(http,app,key,subject,committed.data['sync_cursor'])
        assert second.status_code==200,second.text
        next_ack=await call(app,'communication.sync_checkpoint_ack',
                            {'ack':second.json()['checkpoint_ack']},key=key,subject=subject)
        assert next_ack.status=='ok',wire(next_ack)
        async with app.metadata.transaction(write=False) as tx:
            body=tx.one('SELECT body FROM sync_checkpoints WHERE subject=?',(subject,))[0]
        from msg.core.codec import loads
        assert len(loads(body)['seen'])>=70


@pytest.mark.asyncio
async def test_checkpoint_permission_gain_and_retention_fail_closed(installed):
    app,_=installed
    key,subject,_=await register(app,'checkpoint-stale')
    created=await call(app,'content.post_create',{'parent':'/main','body':'sample'},
                       key=key,subject=subject)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        packet=request_for('communication.sync',{},app.settings.service_url,
                           signer=key,subject=subject,expires_at=NOW+timedelta(seconds=60))
        start=await http.get('/_r/s/start',headers={'x-msg-request':b64(canonical(packet))})
        old=start.json()['sync_cursor']
        opened=await call(app,'communication.sync_checkpoint_open',{'cursor':old},
                          key=key,subject=subject)
        cursor=opened.data['sync_cursor']
        async with app.metadata.transaction(write=True) as tx:
            tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)+1)
        stale=await read_sync(http,app,key,subject,cursor)
        assert stale.status_code==400
        assert stale.json()['error']['code']=='resync_required'
        async with app.metadata.transaction(write=True) as tx:
            tx.set_setting('authorization_epoch',tx.setting('authorization_epoch',0)-1)
            tx.set_setting('sync_floor',10**9)
        expired=await read_sync(http,app,key,subject,cursor)
        assert expired.status_code==400
        assert expired.json()['error']['code']=='resync_required'


@pytest.mark.asyncio
async def test_checkpoint_drains_known_revocations_one_ack_at_a_time(installed):
    app,_=installed
    key,subject,_=await register(app,'checkpoint-revokes')
    created=[]
    for index in range(3):
        post=await call(app,'content.post_create',{'parent':'/main','body':f'known {index}'},
                        key=key,subject=subject)
        created.append(post)
    packet=request_for('communication.sync',{},app.settings.service_url,
                       signer=key,subject=subject,expires_at=NOW+timedelta(seconds=60))
    start=await app.executor.execute(packet)
    opened=await call(app,'communication.sync_checkpoint_open',
                      {'cursor':start.data['sync_cursor']},key=key,subject=subject)
    cursor=opened.data['sync_cursor']
    for post in created:
        rid=post.resources[0].id
        locked=await call(app,'content.chmod',{'id':rid,'mode':'0000'},key=key,
                          subject=subject,expected=((rid,post.data['generation']),))
        assert locked.status=='ok',wire(locked)
    seen=[]
    for _ in range(3):
        page=await call(app,'communication.sync',{'cursor':cursor,'limit':1},
                        key=key,subject=subject)
        assert page.status=='ok',wire(page)
        assert page.data['resync_required'] is True
        assert len(page.data['items'])==1
        item=page.data['items'][0]
        assert set(item)=={'kind','ref'} and item['kind']=='revoked'
        seen.append(item['ref']['id'])
        ack=await call(app,'communication.sync_checkpoint_ack',
                       {'ack':page.data['checkpoint_ack']},key=key,subject=subject)
        assert ack.status=='ok',wire(ack)
        cursor=ack.data['sync_cursor']
    assert set(seen)=={post.resources[0].id for post in created}
    final=await call(app,'communication.sync',{'cursor':cursor,'limit':1},
                     key=key,subject=subject)
    assert final.error.code=='resync_required'
