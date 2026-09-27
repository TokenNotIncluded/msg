"""Share sources are independent and delegated grants track live group membership."""
from datetime import timedelta

import pytest

from msg.core.codec import wire
from test_service import NOW,call,register


async def invoke(app,identity,operation,args,*,version=1,expected=()):
    return await call(app,operation,args,key=identity[0],subject=identity[1],
                      contract_version=version,expected=expected)


def ok(result):
    assert result.status=='ok',(result.error.code if result.error else None,wire(result))
    return result


@pytest.mark.asyncio
async def test_group_reshare_tracks_source_membership_and_independent_grants(installed):
    app,_=installed
    owner=await register(app,'share2-owner')
    member=await register(app,'share2-member')
    recipient=await register(app,'share2-recipient')
    group=ok(await invoke(app,owner,'group.create',{'name':'share2-group'})).resources[0].id
    ok(await invoke(app,owner,'group.invite',{'group':group,'subject':member[1]}))
    ok(await invoke(app,member,'group.join',{'group':group}))
    post=ok(await invoke(app,owner,'content.post_create',
        {'parent':'/main','body':'v2 private'}))
    rid=post.resources[0].id
    ok(await invoke(app,owner,'content.chmod',{'id':rid,'mode':'0600'},
                    expected=((rid,post.data['generation']),)))
    expiry=wire(NOW+timedelta(days=2))
    group_grant=ok(await invoke(app,owner,'sharing.grant',
        {'resource':rid,'grantee':group,'grantee_kind':'group','operations':['read'],
         'expires_at':expiry,'allow_reshare':True},version=2)).data['grant']['id']
    ok(await invoke(app,member,'discovery.get',{'id':rid}))
    missing_source=await invoke(app,member,'sharing.grant',
        {'resource':rid,'grantee':recipient[1],'grantee_kind':'user',
         'operations':['read'],'expires_at':expiry},version=2)
    assert missing_source.error.code=='share_source_required'
    overlong=await invoke(app,member,'sharing.grant',
        {'resource':rid,'grantee':recipient[1],'grantee_kind':'user',
         'operations':['read'],'expires_at':wire(NOW+timedelta(days=3)),
         'source_grant_id':group_grant},version=2)
    assert overlong.error.code=='share_scope_exceeded'
    delegated=ok(await invoke(app,member,'sharing.grant',
        {'resource':rid,'grantee':recipient[1],'grantee_kind':'user',
         'operations':['read'],'expires_at':wire(NOW+timedelta(days=1)),
         'source_grant_id':group_grant},version=2)).data['grant']
    assert delegated['allow_reshare'] is False
    ok(await invoke(app,recipient,'discovery.get',{'id':rid}))
    denied=await invoke(app,recipient,'sharing.grant',
        {'resource':rid,'grantee':member[1],'grantee_kind':'user',
         'operations':['read'],'expires_at':wire(NOW+timedelta(hours=1)),
         'source_grant_id':delegated['id']},version=2)
    assert denied.error.code=='share_source_unavailable'
    ok(await invoke(app,member,'group.leave',{'group':group}))
    for identity in (member,recipient):
        denied=await invoke(app,identity,'discovery.get',{'id':rid})
        assert denied.error.code=='permission_denied',wire(denied)
    direct=ok(await invoke(app,owner,'sharing.grant',
        {'resource':rid,'grantee':recipient[1],'grantee_kind':'user',
         'operations':['read'],'expires_at':expiry},version=2)).data['grant']['id']
    ok(await invoke(app,recipient,'discovery.get',{'id':rid}))
    ok(await invoke(app,owner,'sharing.revoke',{'grant_id':group_grant},version=2))
    ok(await invoke(app,recipient,'discovery.get',{'id':rid}))
    ok(await invoke(app,owner,'sharing.revoke',{'grant_id':direct},version=2))
    denied=await invoke(app,recipient,'discovery.get',{'id':rid})
    assert denied.error.code=='permission_denied'


@pytest.mark.asyncio
async def test_reshare_revocation_and_expiry_do_not_remove_other_sources(installed):
    app,_=installed
    owner=await register(app,'share2-chain-owner')
    intermediary=await register(app,'share2-chain-middle')
    recipient=await register(app,'share2-chain-end')
    post=ok(await invoke(app,owner,'content.post_create',
        {'parent':'/main','body':'chain private'}))
    rid=post.resources[0].id
    ok(await invoke(app,owner,'content.chmod',{'id':rid,'mode':'0600'},
                    expected=((rid,post.data['generation']),)))
    source=ok(await invoke(app,owner,'sharing.grant',
        {'resource':rid,'grantee':intermediary[1],'grantee_kind':'user',
         'operations':['read'],'expires_at':wire(NOW+timedelta(days=2)),
         'allow_reshare':True},version=2)).data['grant']['id']
    child=ok(await invoke(app,intermediary,'sharing.grant',
        {'resource':rid,'grantee':recipient[1],'grantee_kind':'user',
         'operations':['read'],'expires_at':wire(NOW+timedelta(days=1)),
         'source_grant_id':source},version=2)).data['grant']['id']
    independent=ok(await invoke(app,owner,'sharing.grant',
        {'resource':rid,'grantee':recipient[1],'grantee_kind':'user',
         'operations':['read'],'expires_at':wire(NOW+timedelta(hours=1))},
        version=2)).data['grant']['id']
    ok(await invoke(app,recipient,'discovery.get',{'id':rid}))
    ok(await invoke(app,owner,'sharing.revoke',{'grant_id':source},version=2))
    ok(await invoke(app,recipient,'discovery.get',{'id':rid}))
    ok(await invoke(app,owner,'sharing.revoke',{'grant_id':independent},version=2))
    denied=await invoke(app,recipient,'discovery.get',{'id':rid})
    assert denied.error.code=='permission_denied'
    listed=ok(await invoke(app,intermediary,'sharing.list',{'resource':rid},version=2))
    assert listed.data['grants'][0]['id']==child
    app.executor.clock=lambda:NOW+timedelta(days=3)
    denied=await invoke(app,intermediary,'discovery.get',{'id':rid})
    assert denied.error.code=='permission_denied'


@pytest.mark.asyncio
async def test_v2_group_share_does_not_open_parent_or_sensitive_resource(installed):
    app,_=installed
    owner=await register(app,'share2-private-owner')
    member=await register(app,'share2-private-member')
    group=ok(await invoke(app,owner,'group.create',{'name':'share2-private-group'})).resources[0].id
    ok(await invoke(app,owner,'group.invite',{'group':group,'subject':member[1]}))
    ok(await invoke(app,member,'group.join',{'group':group}))
    note=ok(await invoke(app,owner,'identity.note_put',
        {'name':'alone.md','body':'single private note'}))
    rid=note.resources[0].id
    grant=ok(await invoke(app,owner,'sharing.grant',
        {'resource':rid,'grantee':group,'grantee_kind':'group',
         'operations':['read'],'expires_at':wire(NOW+timedelta(days=1))},
        version=2))
    assert grant.data['grant']['status']=='active'
    ok(await invoke(app,member,'discovery.get',{'id':rid}))
    async with app.metadata.transaction(write=False) as tx:
        parent=(await tx.resource(rid)).parent
    denied=await invoke(app,member,'discovery.list',{'parent':parent})
    assert denied.error.code=='permission_denied'
    for kind in ('soul','agents'):
        personal=ok(await invoke(app,owner,'identity.personal_put',
            {'kind':kind,'body':'private'}))
        result=await invoke(app,owner,'sharing.grant',
            {'resource':personal.resources[0].id,'grantee':group,'grantee_kind':'group',
             'operations':['read'],'expires_at':wire(NOW+timedelta(days=1))},version=2)
        assert result.status=='error',wire(result)
