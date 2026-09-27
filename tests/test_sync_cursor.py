"""Independent SyncCursor never doubles as a page cursor or read receipt."""
from datetime import timedelta

import httpx
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.plugins.communication import seal_sync_seen
from msg.transports.http import create_app
from test_service import NOW, call, register


def signed_path(app,key,subject,path,args):
    packet=request_for('communication.sync',args,app.settings.service_url,
                       signer=key,subject=subject,
                       expires_at=NOW+timedelta(seconds=60))
    return path+'/p/'+b64(canonical(packet))


def signed_header(app,key,subject,args):
    packet=request_for('communication.sync',args,app.settings.service_url,
                       signer=key,subject=subject,
                       expires_at=NOW+timedelta(seconds=60))
    return {'x-msg-request':b64(canonical(packet))}


async def state(app):
    async with app.metadata.transaction(write=False) as tx:
        return (tx.one('SELECT COUNT(*) FROM events')[0],
                tx.one('SELECT COUNT(*) FROM jobs')[0],
                tx.one('SELECT COUNT(*) FROM reactions')[0],
                tx.one('SELECT COUNT(*) FROM messages')[0],
                tx.one('SELECT COUNT(*) FROM credentials')[0],
                tx.one('SELECT COUNT(*) FROM revisions')[0],
                tx.one('SELECT SUM(generation) FROM resources')[0],
                tx.one('SELECT COUNT(*) FROM transfers')[0])


@pytest.mark.asyncio
async def test_sync_cursor_tracks_visible_create_modify_archive_and_known_revocation(installed):
    app, _ = installed
    key,user,_=await register(app,'sync-owner')
    first=await call(app,'content.post_create',{'parent':'/main','body':'first'},
                     key=key,subject=user)
    rid=first.resources[0].id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        opened=await http.get(signed_path(app,key,user,'/_read/s/start',{}))
        assert opened.status_code==200,opened.text
        opened_short=await http.get(signed_path(app,key,user,'/_r/s/start',{}))
        assert opened_short.status_code==200
        assert opened.content==opened_short.content
        assert opened.headers['etag']==opened_short.headers['etag']
        assert opened.json()['next'].startswith('/_r/s/')
        assert rid in {item['ref']['id'] for item in opened.json()['items']
                       if item.get('ref')}
        cursor=opened.json()['sync_cursor']
        assert rid not in str(app.cursors.inspect(cursor))
        before=await state(app)
        long=await http.get(signed_path(app,key,user,'/_read/s/'+cursor,{'cursor':cursor}))
        short=await http.get(signed_path(app,key,user,'/_r/s/'+cursor,{'cursor':cursor}))
        assert long.status_code==short.status_code==200
        assert long.content==short.content
        assert long.headers['etag']==short.headers['etag']
        assert long.json()['sync_cursor']==short.json()['sync_cursor']
        long_cursor=app.cursors.inspect(long.json()['sync_cursor'])
        short_cursor=app.cursors.inspect(short.json()['sync_cursor'])
        assert long_cursor['position']['seq']==short_cursor['position']['seq']
        assert long_cursor['query']==short_cursor['query']
        assert long_cursor['position']['seen_ciphertext']==short_cursor['position']['seen_ciphertext']
        assert await state(app)==before
        second=await call(app,'content.post_create',{'parent':'/main','body':'second'},
                          key=key,subject=user)
        new=await http.get(signed_path(app,key,user,opened.json()['next'],{'cursor':cursor}))
        assert new.status_code==200
        assert any(item['kind']=='created' and item['ref']['id']==second.resources[0].id
                   for item in new.json()['items'])
        cursor=new.json()['sync_cursor']
        edited=await call(app,'content.post_edit',
                          {'id':rid,'expected_revision':first.resources[0].revision,'body':'updated'},
                          key=key,subject=user,expected=((rid,first.data['generation']),))
        changed=await http.get(signed_path(app,key,user,'/_r/s/'+cursor,{'cursor':cursor}))
        assert any(item['kind']=='modified' and item['ref']['id']==rid
                   for item in changed.json()['items'])
        cursor=changed.json()['sync_cursor']
        archived=await call(app,'content.archive',{'id':rid},key=key,subject=user,
                            expected=((rid,edited.data['generation']),))
        assert archived.status=='ok'
        archive=await http.get(signed_path(app,key,user,'/_r/s/'+cursor,{'cursor':cursor}))
        assert archive.status_code==400
        assert archive.json()['error']['code']=='resync_required'
        restarted=await http.get(signed_path(app,key,user,'/_r/s/start',{}))
        assert restarted.status_code==200
        assert any(item['kind']=='archived' and item['ref']['id']==rid
                   for item in restarted.json()['items'])
        cursor=restarted.json()['sync_cursor']
        async with app.metadata.transaction(write=False) as tx:
            generation=(await tx.resource(second.resources[0].id)).generation
        locked=await call(app,'content.chmod',
                          {'id':second.resources[0].id,'mode':'0000'},key=key,subject=user,
                          expected=((second.resources[0].id,generation),))
        assert locked.status=='ok'
        revoked=await http.get(signed_path(app,key,user,'/_r/s/'+cursor,{'cursor':cursor}))
        assert revoked.status_code==200
        lost=[item for item in revoked.json()['items'] if item['kind']=='revoked']
        assert lost==[{'kind':'revoked','ref':{'id':second.resources[0].id}}]
        assert revoked.json()['resync_required'] is True
        assert 'sync_cursor' not in revoked.json() and 'next' not in revoked.json()
        assert 'second' not in revoked.text


@pytest.mark.asyncio
async def test_sync_cursor_rejects_other_kinds_expiry_and_expired_window(installed):
    app, _ = installed
    key,user,_=await register(app,'sync-window')
    other_key,other,_=await register(app,'sync-other')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        opened=await http.get(signed_path(app,key,user,'/_r/s/start',{}))
        assert opened.status_code==200
        token=opened.json()['sync_cursor']
        alien=await http.get(signed_path(app,other_key,other,'/_r/s/'+token,{'cursor':token}))
        assert alien.status_code==403
        fake_page=app.cursors.encode('page','x','y')
        kind=await http.get(signed_path(app,key,user,'/_r/s/'+fake_page,{'cursor':fake_page}))
        assert kind.status_code==400
        assert kind.json()['error']['code']=='cursor_kind_mismatch'
        decoded=app.cursors.inspect(token)
        expired=app.cursors.encode('sync-v2',decoded['query'],
            {**decoded['position'],'expires_at':wire(NOW-timedelta(seconds=1))})
        stale=await http.get(signed_path(app,key,user,'/_r/s/'+expired,{'cursor':expired}))
        assert stale.status_code==400
        assert stale.json()['error']['code']=='resync_required'
        async with app.metadata.transaction(write=True) as tx:
            tx.set_setting('sync_floor',decoded['position']['seq']+1)
        window=await http.get(signed_path(app,key,user,'/_r/s/'+token,{'cursor':token}))
        assert window.status_code==400
        assert window.json()['error']['code']=='resync_required'


@pytest.mark.asyncio
async def test_sync_never_emits_unseen_private_reference(installed):
    app, _ = installed
    observer,observer_id,_=await register(app,'sync-observer')
    writer,writer_id,_=await register(app,'sync-writer')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        opened=await http.get(signed_path(app,observer,observer_id,'/_r/s/start',{}))
        token=opened.json()['sync_cursor']
        post=await call(app,'content.post_create',{'parent':'/main','body':'unseen secret'},
                        key=writer,subject=writer_id)
        rid=post.resources[0].id
        await call(app,'content.chmod',{'id':rid,'mode':'0600'},key=writer,
                   subject=writer_id,expected=((rid,post.data['generation']),))
        before=await state(app)
        result=await http.get(signed_path(app,observer,observer_id,'/_r/s/'+token,
                                          {'cursor':token}))
        assert result.status_code==400
        assert result.json()['error']['code']=='resync_required'
        assert rid not in result.text and 'unseen secret' not in result.text
        assert await state(app)==before


@pytest.mark.asyncio
async def test_sync_seen_window_has_a_hard_bounded_size(installed):
    app, _ = installed
    with pytest.raises(Failure,match='resync_required'):
        seal_sync_seen(app,[f'r_{index}' for index in range(65)])


@pytest.mark.asyncio
async def test_sync_permission_gain_requires_replay_of_old_events(installed):
    app, _ = installed
    observer,observer_id,_=await register(app,'sync-gain-observer')
    writer,writer_id,_=await register(app,'sync-gain-writer')
    watched=await call(app,'communication.watch',{'id':'/main'},
                       key=observer,subject=observer_id)
    assert watched.status=='ok'
    post=await call(app,'content.post_create',{'parent':'/main','body':'old private body'},
                    key=writer,subject=writer_id)
    rid=post.resources[0].id
    private=await call(app,'content.chmod',{'id':rid,'mode':'0600'},key=writer,
                       subject=writer_id,expected=((rid,post.data['generation']),))
    assert private.status=='ok'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        opened=await http.get(signed_path(app,observer,observer_id,'/_r/s/start',{}))
        assert opened.status_code==200
        assert rid not in opened.text
        cursor=opened.json()['sync_cursor']
        public=await call(app,'content.chmod',{'id':rid,'mode':'0644'},key=writer,
                          subject=writer_id,expected=((rid,private.data['generation']),))
        assert public.status=='ok'
        before=await state(app)
        stale=await http.get(signed_path(app,observer,observer_id,'/_r/s/'+cursor,
                                         {'cursor':cursor}))
        assert stale.status_code==400
        assert stale.json()['error']['code']=='resync_required'
        assert rid not in stale.text and 'old private body' not in stale.text
        assert await state(app)==before
        restarted=await http.get(signed_path(app,observer,observer_id,'/_r/s/start',{}))
        assert restarted.status_code==200
        # A fresh bounded replay can discover the now-visible old event.
        assert rid in restarted.text


@pytest.mark.asyncio
async def test_sync_over_64_refs_fails_without_advancing_cursor(installed):
    app, _ = installed
    key,user,_=await register(app,'sync-over-64')
    for index in range(65):
        post=await call(app,'content.post_create',{'parent':'/main','body':f'item {index}'},
                        key=key,subject=user)
        assert post.status=='ok'
    before=await state(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        first=await http.get(signed_path(app,key,user,'/_r/s/start',{}))
        assert first.status_code==200
        cursor=first.json()['sync_cursor']
        over=await http.get('/_r/s/'+cursor,
                            headers=signed_header(app,key,user,{'cursor':cursor}))
        assert over.status_code==400,over.text
        assert over.json()['error']['code']=='resync_required'
        assert 'sync_cursor' not in over.json() and 'next' not in over.json()
        # Replaying from the beginning is not a long-term recovery strategy:
        # the same exact seen-set boundary is reached again.  Keep this as a
        # regression until a /-/ checkpoint operation can persist exact state.
        restarted=await http.get(signed_path(app,key,user,'/_r/s/start',{}))
        assert restarted.status_code==200
        replay_cursor=restarted.json()['sync_cursor']
        replay=await http.get('/_r/s/'+replay_cursor,
                              headers=signed_header(app,key,user,{'cursor':replay_cursor}))
        assert replay.status_code==400
        assert replay.json()['error']['code']=='resync_required'
        assert 'sync_cursor' not in replay.json() and 'next' not in replay.json()
    assert await state(app)==before


@pytest.mark.asyncio
async def test_sync_topic_membership_change_invalidates_old_cursor(installed):
    app, _ = installed
    key,user,_=await register(app,'sync-topic-member')
    creator,creator_id,_=await register(app,'sync-topic-creator')
    topic=await call(app,'content.topic_create',{'parent':'/main','name':'sync-topic'},
                     key=creator,subject=creator_id)
    assert topic.status=='ok'
    rid=topic.resources[0].id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        opened=await http.get(signed_path(app,key,user,'/_r/s/start',{}))
        assert opened.status_code==200
        cursor=opened.json()['sync_cursor']
        joined=await call(app,'content.topic_join',{'id':rid},key=key,subject=user)
        assert joined.status=='ok'
        before=await state(app)
        stale=await http.get(signed_path(app,key,user,'/_r/s/'+cursor,{'cursor':cursor}))
        assert stale.status_code==400
        assert stale.json()['error']['code']=='resync_required'
        assert await state(app)==before
        restarted=await http.get(signed_path(app,key,user,'/_r/s/start',{}))
        assert restarted.status_code==200
