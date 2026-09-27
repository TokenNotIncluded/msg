"""Personal notes and todos have explicit private, conflict-checked lifecycles."""
import pytest

from msg.core.codec import wire
from test_service import call, register


@pytest.mark.asyncio
async def test_note_archive_restore_reject_stale_versions_and_keep_private(installed):
    app,_=installed
    key,owner,_=await register(app,'note-lifecycle')
    other_key,other,_=await register(app,'note-outsider')
    note=await call(app,'identity.note_put',{'name':'plan','body':'Keep this private.'},
                    key=key,subject=owner)
    assert note.status=='ok',wire(note)
    rid=note.resources[0].id
    for operation,args in (
        ('content.archive',{'id':rid}),
        ('content.chmod',{'id':rid,'mode':'0644'}),
        ('content.move',{'id':rid,'parent':'/main','name':'plan-public'}),
    ):
        bypass=await call(app,operation,args,key=key,subject=owner,
                          expected=((rid,note.data['generation']),))
        assert bypass.status=='error',wire(bypass)
    stale=await call(app,'identity.note_archive',{'name':'plan','expected_revision':'v_stale'},
                     key=key,subject=owner,expected=((rid,note.data['generation']),))
    assert stale.error.code=='revision_conflict'
    stale=await call(app,'identity.note_archive',{'name':'plan',
                     'expected_revision':note.resources[0].revision},key=key,subject=owner,
                     expected=((rid,note.data['generation']-1),))
    assert stale.error.code=='generation_conflict'
    archived=await call(app,'identity.note_archive',{'name':'plan',
        'expected_revision':note.resources[0].revision},key=key,subject=owner,
        expected=((rid,note.data['generation']),))
    assert archived.status=='ok' and archived.data['state']=='archived'
    assert (await call(app,'identity.note_get',{'name':'plan'},key=key,subject=owner)).error.code=='note_not_found'
    assert not (await call(app,'identity.note_list',{},key=key,subject=owner)).data['items']
    rejected=await call(app,'identity.note_put',{'name':'plan','body':'Should not revive.',
        'expected_revision':note.resources[0].revision},key=key,subject=owner,
        expected=((rid,archived.data['generation']),))
    assert rejected.status=='error'
    stale=await call(app,'identity.note_restore',{'name':'plan',
        'expected_revision':note.resources[0].revision},key=key,subject=owner,
        expected=((rid,note.data['generation']),))
    assert stale.error.code=='generation_conflict'
    restored=await call(app,'identity.note_restore',{'name':'plan',
        'expected_revision':note.resources[0].revision},key=key,subject=owner,
        expected=((rid,archived.data['generation']),))
    assert restored.status=='ok' and restored.data['state']=='active'
    assert (await call(app,'identity.note_get',{'name':'plan'},key=key,subject=owner)).status=='ok'
    assert (await call(app,'identity.note_get',{'name':'plan'},key=other_key,subject=other)).status=='error'
    assert (await call(app,'discovery.get',{'id':rid},key=other_key,subject=other)).status=='error'


@pytest.mark.asyncio
async def test_todo_private_lifecycle_paging_and_related_access(installed):
    app,_=installed
    key,owner,_=await register(app,'todo-owner')
    other_key,other,_=await register(app,'todo-other')
    before=await call(app,'identity.todo_list',{},key=key,subject=owner)
    assert before.status=='ok' and not before.data['items']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE parent=? AND name='todos'",(owner,))[0]==0
    private=await call(app,'identity.todo_put',{'name':'private','title':'Private work',
        'related_resource':{'id':'/private'}},key=key,subject=owner)
    assert private.status=='error'
    first=await call(app,'identity.todo_put',{'name':'alpha','title':'Draft plan',
        'description':'Review with care.','due_at':'2026-10-01T09:00:00Z',
        'related_resource':{'id':'/main'}},key=key,subject=owner)
    assert first.status=='ok',wire(first)
    second=await call(app,'identity.todo_put',{'name':'beta','title':'Finish plan'},
                      key=key,subject=owner)
    assert second.status=='ok',wire(second)
    assert second.data['status']=='pending' and second.data['priority']=='neutral'
    rid=first.resources[0].id
    for operation,args in (
        ('content.archive',{'id':rid}),
        ('content.chmod',{'id':rid,'mode':'0644'}),
        ('content.move',{'id':rid,'parent':'/main','name':'todo-public'}),
    ):
        bypass=await call(app,operation,args,key=key,subject=owner,
                          expected=((rid,first.data['generation']),))
        assert bypass.status=='error',wire(bypass)
    page=await call(app,'identity.todo_list',{'limit':1},key=key,subject=owner)
    assert len(page.data['items'])==1 and page.data['next_after_name']=='alpha'
    assert page.data['items'][0]['title']=='Draft plan'
    assert page.data['items'][0]['status']=='pending'
    assert page.data['items'][0]['due_at']=='2026-10-01T09:00:00.000000Z'
    page2=await call(app,'identity.todo_list',{'limit':1,'after_name':'alpha'},key=key,subject=owner)
    assert [item['name'] for item in page2.data['items']]==['beta']
    own=await call(app,'identity.todo_get',{'name':'alpha'},key=key,subject=owner)
    assert own.status=='ok' and own.data['title']=='Draft plan'
    assert own.data['related_resource']['id']=='t_main'
    assert (await call(app,'identity.todo_get',{'name':'alpha'},key=other_key,subject=other)).status=='error'
    assert (await call(app,'discovery.get',{'id':rid},key=other_key,subject=other)).status=='error'
    assert (await call(app,'discovery.search',{'query':'Draft plan'},
                       key=other_key,subject=other)).data.get('results',[])==[]
    stale=await call(app,'identity.todo_put',{'name':'alpha','title':'Changed',
        'expected_revision':'v_old'},key=key,subject=owner,
        expected=((rid,first.data['generation']),))
    assert stale.error.code=='revision_conflict'
    revised=await call(app,'identity.todo_put',{'name':'alpha','title':'Changed',
        'status':'in_progress','priority':'high',
        'expected_revision':first.resources[0].revision},key=key,subject=owner,
        expected=((rid,first.data['generation']),))
    assert revised.status=='ok',wire(revised)
    archive=await call(app,'identity.todo_archive',{'name':'alpha',
        'expected_revision':revised.resources[0].revision},key=key,subject=owner,
        expected=((rid,revised.data['generation']),))
    assert archive.status=='ok'
    assert (await call(app,'identity.todo_get',{'name':'alpha'},key=key,subject=owner)).status=='error'
    assert [item['name'] for item in (await call(app,'identity.todo_list',{},key=key,subject=owner)).data['items']]==['beta']
    restored=await call(app,'identity.todo_restore',{'name':'alpha',
        'expected_revision':revised.resources[0].revision},key=key,subject=owner,
        expected=((rid,archive.data['generation']),))
    assert restored.status=='ok'
    again=await call(app,'identity.todo_get',{'name':'alpha'},key=key,subject=owner)
    assert again.status=='ok' and again.data['status']=='in_progress' and again.data['priority']=='high'
    async with app.metadata.transaction(write=False) as tx:
        directory=tx.one("SELECT id,mode FROM resources WHERE parent=? AND name='todos'",(owner,))
        resource=await tx.resource(rid)
        assert directory[1]==0o700 and resource.mode==0o600
