"""One source table exercised through the actual transports and CLI dispatcher."""
from datetime import timedelta

import httpx
import pytest

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import canonical, loads, wire
from msg.core.requests import request_for
from msg.transports.client import HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport
from msg.transports.http import create_app
from test_authorization import approve, scoped
from test_authorization_sources import private_post, grant
from test_service import NOW, call, register
from read_only_evidence import readonly_evidence


SOURCES = ('owner','mode','inheritance','group-member','group-maintainer','group-owner',
           'certificate','share-user','share-group')
ADAPTERS = (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport)


def ok(result):
    assert result.status == 'ok', wire(result)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize('source', SOURCES)
async def test_live_source_loss_across_transports_cli_and_projections(installed,tmp_path,monkeypatch,capsys,source):
    app,root = installed
    owner = await register(app,'matrix-owner')
    reader = await register(app,'matrix-reader')
    writer = reader if source == 'owner' else owner
    rid,revision = await private_post(app,writer)
    certs = ()
    group = None

    async def invoke(operation,args,identity=owner,**kw):
        return ok(await call(app,operation,args,key=identity[0],subject=identity[1],**kw))

    async def change(operation,args,identity=owner,**kw):
        async with app.metadata.transaction(write=False) as tx:
            generation = (await tx.resource(args['id'])).generation
        return await invoke(operation,args,identity,expected=((args['id'],generation),),**kw)

    if source in {'mode','inheritance'}:
        await change('content.chmod',{'id':rid,'mode':'0644'})
        if source == 'inheritance':
            folder = (await invoke('content.topic_create',{'parent':'/main','name':'inherited'})).resources[0].id
            # A live parent link determines traversal even for an old revision.
            async with app.metadata.transaction(write=True) as tx:
                from dataclasses import replace
                resource = await tx.resource(rid)
                await tx.replace(replace(resource,parent=folder,generation=resource.generation+1),resource.generation)
    if source.startswith('group-') or source == 'share-group':
        creator = reader if source == 'group-owner' else owner
        invited = owner if source == 'group-owner' else reader
        group = (await invoke('group.create',{'name':'matrix-group'},creator)).resources[0].id
        await invoke('group.invite',{'group':group,'subject':invited[1]},creator)
        await invoke('group.join',{'group':group},invited)
        if source == 'group-maintainer':
            await invoke('group.role_set',{'group':group,'subject':reader[1],'role':'maintainer'})
        if source == 'share-group':
            await grant(app,owner,rid,group,kind='group')
        else:
            await change('content.chgrp',{'id':rid,'group':group})
            await change('content.chmod',{'id':rid,'mode':'0640'})
    elif source == 'share-user':
        gid = await grant(app,owner,rid,reader[1])
    elif source == 'certificate':
        ops = ('discovery.get@1','discovery.search@1','communication.sync@1',
               'communication.inbox@1','communication.outbox@1')
        cert = await approve(app,root,reader[1],reader[0],(
            scoped(app,'resource.read_override',rid,ops),))
        certs = (cert.resource_id,)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        adapters = [kind(app.settings.service_url,http=http) for kind in ADAPTERS]
        state = ClientState(tmp_path/'client',server=app.settings.service_url)
        state.save_signer(reader[0])
        state.data.update(subject_id=reader[1],certificates=list(certs));state._save()
        monkeypatch.setitem(cli.TRANSPORTS,'http',lambda server:HTTPTransport(server,http=http))
        monkeypatch.setattr(cli,'MsgClient',lambda saved,adapter:MsgClient(saved,adapter,clock=lambda:NOW))

        async def matrix(expected):
            async with readonly_evidence(app, monkeypatch):
                for args in ({'id':rid},{'id':rid,'view':'history'},{'id':rid,'revision':revision}):
                    packet = request_for('discovery.get',args,app.settings.service_url,
                        signer=reader[0],subject=reader[1],certificates=certs,source='msg',
                        expires_at=NOW+timedelta(seconds=60))
                    for adapter in adapters:
                        result = await adapter.call(packet)
                        assert result.status == expected,(source,adapter.name,args,wire(result))
                parsed = cli.parser().parse_args(['--config-dir',str(state.directory),'--server',
                    app.settings.service_url,'call','discovery.get',canonical({'id':rid}).decode()])
                result = await cli.run(parsed)
                rendered = loads(capsys.readouterr().out.strip())
                assert (result == 0) == (expected == 'ok'),rendered
                assert rendered['status'] == expected,rendered

        await matrix('ok')
        # Queue an actual reference. Notification projections must not retain
        # it after access is lost, independently of payload/history reads.
        await invoke('communication.send',{'recipient':reader[1],
            'resource':{'id':rid,'revision':revision}},writer)
        if source == 'owner':
            authority = await approve(app,root,reader[1],reader[0],(
                scoped(app,'resource.chown',rid,('content.chown@1',)),))
            await change('content.chown',{'id':rid,'owner':owner[1]},reader,certs=(authority.resource_id,))
        elif source == 'mode':
            await change('content.chmod',{'id':rid,'mode':'0600'})
        elif source == 'inheritance':
            await change('content.chmod',{'id':folder,'mode':'0700'})
        elif source == 'group-owner':
            await invoke('group.owner_transfer',{'group':group,'subject':owner[1]},reader)
            await matrix('ok')  # Losing a role does not remove active membership.
            await invoke('group.leave',{'group':group},reader)
        elif source.startswith('group-') or source == 'share-group':
            await invoke('group.leave',{'group':group},reader)
        elif source == 'share-user':
            await invoke('sharing.revoke',{'grant_id':gid},contract_version=2)
        else:
            authority = await approve(app,root,owner[1],owner[0],(
                scoped(app,'cert.revoke',cert.resource_id,('cert.revoke@1',)),))
            await invoke('cert.revoke',{'id':cert.resource_id,'reason':'matrix revocation'},certs=(authority.resource_id,))
        await matrix('error')
        async with readonly_evidence(app, monkeypatch):
            # Drop a revoked certificate before testing ordinary projections: the
            # revoked envelope itself has already been rejected by every adapter.
            for operation,args in (('discovery.search',{'query':'source-matrix-private'}),
                ('communication.sync',{}),('communication.inbox',{}),('communication.outbox',{})):
                packet = request_for(operation,args,app.settings.service_url,signer=reader[0],subject=reader[1],
                    source='msg',expires_at=NOW+timedelta(seconds=60))
                for adapter in adapters:
                    result = ok(await adapter.call(packet))
                    assert 'source-matrix-private' not in canonical(result.data).decode(),(source,operation,wire(result))
                    if operation != 'communication.sync':
                        assert rid not in canonical(result.data).decode(),(source,operation,wire(result))
