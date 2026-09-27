"""Long text reads pin a revision and never mark content as read."""
from datetime import timedelta

import httpx
import pytest

from msg.application import Application
from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app
from test_service import NOW, call, register


async def business_state(app):
    async with app.metadata.transaction(write=False) as tx:
        return (tx.one('SELECT COUNT(*) FROM events')[0],
                tx.one('SELECT COUNT(*) FROM jobs')[0],
                tx.one('SELECT COUNT(*) FROM revisions')[0],
                tx.one('SELECT SUM(generation) FROM resources')[0])


@pytest.mark.asyncio
async def test_read_cursor_keeps_revision_and_splits_markdown_on_blocks_or_utf8(installed):
    app, _ = installed
    key, user, _ = await register(app, 'long-reader')
    body='# Alpha\n\nFirst paragraph.\n\n## Beta\n\n' + '你好世界🙂'*80 + '\n\nLast paragraph.\n'
    created=await call(app,'content.post_create',{'parent':'/main','body':body},key=key,subject=user)
    rid=created.resources[0].id
    original_revision=created.resources[0].revision
    before=await business_state(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        first=await http.get(f'/_read/{rid}/read?max_bytes=80')
        assert first.status_code==200,first.text
        chunk=first.json()
        assert chunk['revision']==original_revision
        assert chunk['text'].endswith('\n\n')
        assert chunk['next'].startswith('/_r/c/')
        assert await business_state(app)==before
        fresh=Application(app.settings,clock=lambda:NOW)
        await fresh.load()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(fresh)),
                                         base_url='http://testserver') as resumed:
                disconnected=await resumed.get(chunk['next'])
                assert disconnected.status_code==200
                assert disconnected.json()['revision']==original_revision
        finally:
            await fresh.close()

        edited=await call(app,'content.post_edit',
                          {'id':rid,'expected_revision':original_revision,'body':'replacement'},
                          key=key,subject=user,expected=((rid,created.data['generation']),))
        assert edited.status=='ok'
        after_edit=await business_state(app)
        collected=[chunk['text']]
        saw_large_block=False
        next_path=chunk['next']
        while next_path:
            token=next_path.removeprefix('/_r/c/')
            short=await http.get('/_r/c/'+token)
            long=await http.get('/_read/c/'+token)
            assert short.status_code==long.status_code==200
            assert short.json()==long.json()
            value=short.json()
            assert value['revision']==original_revision
            value['text'].encode('utf-8')
            saw_large_block |= value['continued_block']
            collected.append(value['text'])
            next_path=value.get('next')
            assert len(collected)<50
        assert ''.join(collected)==body
        assert saw_large_block
        newest=await http.get(f'/_r/{rid}/read?max_bytes=80')
        assert newest.status_code==200
        assert newest.json()['revision']==edited.resources[0].revision
        assert newest.json()['text']=='replacement'
        assert await business_state(app)==after_edit


@pytest.mark.asyncio
async def test_read_cursor_prev_expiry_and_live_permission_recheck(installed):
    app, _ = installed
    key, user, _ = await register(app, 'read-private')
    body='## One\n\n'+'a'*100+'\n\n## Two\n\n'+'b'*100
    created=await call(app,'content.post_create',{'parent':'/main','body':body},key=key,subject=user)
    rid=created.resources[0].id
    await call(app,'content.chmod',{'id':rid,'mode':'0600'},key=key,subject=user,
               expected=((rid,created.data['generation']),))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        args={'id':rid,'max_bytes':64}
        signed=request_for('discovery.read_segment',args,app.settings.service_url,
                           signer=key,subject=user,expires_at=NOW+timedelta(seconds=120))
        first=await http.get(f'/_read/{rid}/read?max_bytes=64',
                             headers={'X-Msg-Request':b64(canonical(signed))})
        assert first.status_code==200,first.text
        cursor=first.json()['next'].removeprefix('/_r/c/')
        broken=cursor[:-1]+('A' if cursor[-1]!='A' else 'B')
        tampered=await http.get('/_r/c/'+broken)
        assert tampered.status_code==400
        assert tampered.json()['error']['code']=='invalid_cursor'
        anonymous=await http.get('/_r/c/'+cursor)
        assert anonymous.status_code in {400,403}
        assert anonymous.json()['error']['code'] in {'cursor_principal_mismatch','permission_denied'}
        continued=request_for('discovery.read_segment',{'cursor':cursor},app.settings.service_url,
                              signer=key,subject=user,expires_at=NOW+timedelta(seconds=120))
        second=await http.get('/_r/c/'+cursor,
                              headers={'X-Msg-Request':b64(canonical(continued))})
        assert second.status_code==200,second.text
        prev=second.json()['prev']
        previous_token=prev.removeprefix('/_r/c/')
        back=request_for('discovery.read_segment',{'cursor':previous_token},app.settings.service_url,
                         signer=key,subject=user,expires_at=NOW+timedelta(seconds=120))
        previous=await http.get(prev,headers={'X-Msg-Request':b64(canonical(back))})
        assert previous.status_code==200
        assert previous.json()['text']==first.json()['text']
        changed=await call(app,'content.chmod',{'id':rid,'mode':'0000'},key=key,subject=user,
                           expected=((rid,created.data['generation']+1),))
        assert changed.status=='ok'
        denied=await http.get('/_r/c/'+cursor,
                              headers={'X-Msg-Request':b64(canonical(continued))})
        assert denied.status_code==403
        app.clock=lambda: NOW+timedelta(minutes=16)
        expired=await http.get('/_r/c/'+cursor)
        assert expired.status_code==400
        assert expired.json()['error']['code']=='cursor_expired'


@pytest.mark.asyncio
async def test_read_cursor_keeps_fenced_code_list_and_table_blocks_together(installed):
    app, _ = installed
    key, user, _ = await register(app, 'block-reader')
    code='```py\nx=1\n\nx=2\n```\n\n'
    listing='- one\n- two\n\n'
    table='|a|b|\n|---|---|\n|1|2|\n'
    body='Intro.\n\n'+code+listing+table
    created=await call(app,'content.post_create',{'parent':'/main','body':body},key=key,subject=user)
    rid=created.resources[0].id
    chunks=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        path=f'/_read/{rid}/read?max_bytes=64'
        while path:
            response=await http.get(path)
            assert response.status_code==200,response.text
            value=response.json()
            chunks.append(value['text'])
            path=value.get('next')
    assert ''.join(chunks)==body
    assert any(code in chunk for chunk in chunks)
    assert any(listing in chunk for chunk in chunks)
    assert any(table in chunk for chunk in chunks)
