"""Subject collaboration paths and CLI reuse the read/write Operation boundary."""
from datetime import timedelta

import httpx
import pytest

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import b64, canonical, loads, wire
from msg.core.requests import request_for
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from test_service import NOW, call, register


def _headers(app, operation, args, key, subject):
    packet=request_for(operation,args,app.settings.service_url,signer=key,
                       subject=subject,expires_at=NOW+timedelta(seconds=120))
    return {'X-Msg-Request':b64(canonical(packet))}


@pytest.mark.asyncio
async def test_subject_handoffs_leases_paths_are_read_only_and_acl_filtered(installed):
    app,_=installed
    alice_key,alice,_=await register(app,'collab-path-alice')
    bob_key,bob,_=await register(app,'collab-path-bob')
    private=await call(app,'identity.note_put',
                       {'name':'handoff-context','body':'Private context'},
                       key=alice_key,subject=alice)
    rid=private.resources[0].id
    handoff=await call(app,'communication.handoff_create',
                       {'to_subject':bob,'resource_refs':[rid]},
                       key=alice_key,subject=alice)
    hid=handoff.data['handoff']['id']
    post=await call(app,'content.post_create',{'parent':'/main','body':'Public task'},
                    key=bob_key,subject=bob)
    lease=await call(app,'communication.lease_acquire',
                     {'target':post.resources[0].id,'purpose':'draft',
                      'expires_at':wire(NOW+timedelta(hours=1))},
                     key=bob_key,subject=bob)
    lid=lease.data['lease']['id']
    async with app.metadata.transaction(write=False) as tx:
        before=tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in
                     ('handoffs','collaboration_leases','messages','events','audit'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        for kind,operation,item_id in (('handoffs','handoff',hid),
                                        ('leases','lease',lid)):
            list_headers=_headers(app,'communication.'+operation+'_list',{},bob_key,bob)
            listed=await http.get(f'/@collab-path-bob/{kind}/',headers=list_headers)
            assert listed.status_code==200,listed.text
            assert listed.json()['items'][0]['id']==item_id
            assert listed.json()['path']==f'/@collab-path-bob/{kind}'
            item_headers=_headers(app,'communication.'+operation+'_get',
                                  {'id':item_id},bob_key,bob)
            item=await http.get(f'/@collab-path-bob/{kind}/{item_id}',
                                headers=item_headers)
            assert item.status_code==200,item.text
            assert item.json()[operation]['id']==item_id
            head=await http.head(f'/@collab-path-bob/{kind}/{item_id}',
                                 headers=item_headers)
            assert head.status_code==200 and head.headers['etag']==item.headers['etag']
            cached=await http.get(f'/@collab-path-bob/{kind}/{item_id}',
                                  headers={**item_headers,'If-None-Match':item.headers['etag']})
            assert cached.status_code==304
            cross=await http.get(f'/@collab-path-alice/{kind}/',headers=list_headers)
            assert cross.status_code==403
        handoff_headers=_headers(app,'communication.handoff_get',{'id':hid},bob_key,bob)
        filtered=await http.get(f'/@collab-path-bob/handoffs/{hid}',
                                headers=handoff_headers)
        assert filtered.json()['handoff']['resource_refs']==[]
        assert rid not in filtered.text and 'Private context' not in filtered.text
        grant=await call(app,'sharing.grant',
                         {'resource':rid,'grantee':bob,
                          'expires_at':wire(NOW+timedelta(days=1))},
                         key=alice_key,subject=alice)
        assert grant.status=='ok',wire(grant)
        visible=await http.get(f'/@collab-path-bob/handoffs/{hid}',
                               headers=handoff_headers)
        assert visible.json()['handoff']['resource_refs']==[rid]
        revoked=await call(app,'sharing.revoke',
                           {'grant_id':grant.data['grant']['id']},
                           key=alice_key,subject=alice)
        assert revoked.status=='ok'
        recached=await http.get(f'/@collab-path-bob/handoffs/{hid}',
                                headers={**handoff_headers,
                                         'If-None-Match':visible.headers['etag']})
        assert recached.status_code==200
        assert recached.json()['handoff']['resource_refs']==[]
        assert recached.headers['etag']!=visible.headers['etag']
        async with app.metadata.transaction(write=False) as tx:
            before=tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in
                         ('handoffs','collaboration_leases','messages','events','audit'))
        for path in (f'/@collab-path-bob/handoffs/{hid}',
                     f'/@collab-path-bob/leases/{lid}'):
            denied=await http.post(path,json={})
            assert denied.status_code in {404,405}
    async with app.metadata.transaction(write=False) as tx:
        after=tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in
                    ('handoffs','collaboration_leases','messages','events','audit'))
    assert after==before


@pytest.mark.asyncio
async def test_cli_handoff_and_lease_workflows_use_public_operations(installed,tmp_path,monkeypatch,capsys):
    app,_=installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        monkeypatch.setitem(cli.TRANSPORTS,'http',
                            lambda server:HTTPTransport(server,http=http))
        alice_dir,bob_dir=tmp_path/'alice',tmp_path/'bob'
        alice=MsgClient(ClientState(alice_dir,server=app.settings.service_url),
                        HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
        bob=MsgClient(ClientState(bob_dir,server=app.settings.service_url),
                      HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
        assert (await alice.register('cli-handoff-a')).status=='ok'
        assert (await bob.register('cli-handoff-b')).status=='ok'
        monkeypatch.setattr(cli,'MsgClient',lambda state,transport:
                            MsgClient(state,transport,clock=lambda:NOW))

        async def invoke(directory,*command):
            args=cli.parser().parse_args(['--config-dir',str(directory),
                '--server',app.settings.service_url,*command])
            status=await cli.run(args)
            output=loads(capsys.readouterr().out.encode().strip())
            return status,output

        status,created=await invoke(alice_dir,'handoff','create',bob.state.subject,
                                    '--message','Please continue')
        assert status==0 and created['status']=='ok'
        hid=created['data']['handoff']['id']
        status,listed=await invoke(bob_dir,'handoff','list')
        assert status==0 and listed['data']['items'][0]['id']==hid
        status,got=await invoke(bob_dir,'handoff','get',hid)
        assert status==0 and got['data']['handoff']['status']=='pending'
        status,accepted=await invoke(bob_dir,'handoff','accept',hid,
                                     '--generation','1')
        assert status==0 and accepted['data']['handoff']['status']=='accepted'
        status,failed=await invoke(alice_dir,'handoff','cancel',hid,
                                   '--generation','1')
        assert status==1 and failed['status']=='error'
        post=await bob.call('content.post_create',{'parent':'/main','body':'CLI lease target'})
        target=post.resources[0].id
        expiry=wire(NOW+timedelta(hours=1))
        status,acquired=await invoke(alice_dir,'lease','acquire',target,
                                     '--purpose','draft','--expires-at',expiry)
        assert status==0 and acquired['status']=='ok'
        lid=acquired['data']['lease']['id']
        status,leases=await invoke(alice_dir,'lease','list')
        assert status==0 and leases['data']['items'][0]['id']==lid
        status,got=await invoke(alice_dir,'lease','get',lid)
        assert status==0 and got['data']['lease']['target']==target
        status,renewed=await invoke(alice_dir,'lease','renew',lid,
                                    '--generation','1',
                                    '--expires-at',wire(NOW+timedelta(hours=2)))
        assert status==0 and renewed['data']['lease']['generation']==2
        status,released=await invoke(alice_dir,'lease','release',lid,
                                     '--generation','2')
        assert status==0 and released['data']['lease']['status']=='released'
