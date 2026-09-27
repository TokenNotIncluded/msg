import pytest
from msg.core.codec import wire
from test_service import register,call

@pytest.mark.asyncio
async def test_history_mailbox_thread_and_ack_pagination(installed):
    app,_=installed
    key,uid,_=await register(app,'pager')
    other,oid,_=await register(app,'reader')
    post=await call(app,'content.post_create',{'parent':'/main','body':'root'},key=key,subject=uid)
    root=post.resources[0].id
    replies=[]
    for index in range(3):
        reply=await call(app,'discussion.reply',{'target':{'id':root},'body':str(index)},key=other,subject=oid)
        assert reply.status=='ok',wire(reply)
        replies.append(reply.resources[0].id)
        await call(app,'communication.send',{'recipient':uid,'resource':wire(reply.resources[0])},key=other,subject=oid)
    first=await call(app,'communication.inbox',{'limit':1},key=key,subject=uid)
    assert len(first.data['items'])==1 and first.data['next_requires_auth']
    seen=[first.data['items'][0]['id']]
    while first.data.get('cursor'):
        first=await call(app,'communication.inbox',{'limit':1,'cursor':first.data['cursor']},key=key,subject=uid)
        seen.extend(x['id'] for x in first.data['items'])
    assert len(set(seen))==3
    thread=await call(app,'discussion.thread',{'id':root,'limit':1})
    assert thread.status=='ok' and thread.data.get('cursor'),wire(thread)
    found=[]
    while True:
        found.extend(x['id'] for x in thread.data['items'])
        if not thread.data.get('cursor'):break
        thread=await call(app,'discussion.thread',{'id':root,'limit':1,'cursor':thread.data['cursor']})
    assert sorted(found)==sorted([root,*replies])

@pytest.mark.asyncio
async def test_sync_acl_change_requires_resync_not_empty_history(installed):
    app,_=installed
    key,uid,_=await register(app,'sync-agent')
    item=await call(app,'content.post_create',{'parent':'/main','body':'sync'},key=key,subject=uid)
    baseline=await call(app,'communication.changes',{},key=key,subject=uid)
    assert baseline.status=='ok'
    changed=await call(app,'content.chmod',{'id':item.resources[0].id,'mode':'0600'},key=key,subject=uid,
                       expected=((item.resources[0].id,item.data['generation']),))
    assert changed.status=='ok'
    old=await call(app,'communication.changes',{'cursor':baseline.data['sync_cursor']},key=key,subject=uid)
    assert old.error.code=='resync_required'
