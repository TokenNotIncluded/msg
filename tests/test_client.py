from datetime import timedelta
import httpx
import pytest
from msg.core.codec import canonical, digest
from msg.core.errors import Failure
from msg.client import MsgClient, ClientState
from msg.transports.client import HTTPTransport, PathGETTransport, MCPHTTPTransport
from msg.transports.http import create_app
from test_service import NOW


@pytest.mark.asyncio
@pytest.mark.parametrize('transport_type',[HTTPTransport,PathGETTransport,MCPHTTPTransport])
async def test_client_register_sign_transfer_and_cross_transport_resume(installed,tmp_path,transport_type):
    app,_=installed
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),base_url=app.settings.service_url)
    transport=transport_type(app.settings.service_url,http=http)
    state=ClientState(tmp_path/'client',server=app.settings.service_url)
    client=MsgClient(state,transport,clock=lambda:NOW)
    result=await client.register('client-agent')
    assert result.status=='ok',result
    assert state.subject==result.subject
    assert state.signer.key_id==result.data['key_id']
    assert state.key_path.stat().st_mode & 0o777==0o600
    assert state.age_key_path.stat().st_mode & 0o777==0o600
    assert state.encryption_recipient==result.data['encryption_recipient']
    post=await client.call('content.post_create',{'parent':'/main','body':'signed by client'})
    assert post.status=='ok' and post.prefer_cli is False,post
    assert state.subject==post.actor
    # One protocol opens and uploads; another protocol resumes the same session.
    source=tmp_path/'source.bin'; source.write_bytes(bytes(range(256))*19)
    opened=await client.call('transfer.open',{'direction':'upload','size':source.stat().st_size,'digest':digest(source.read_bytes()),'requested_part_bytes':89})
    tid=opened.data['transfer_id']
    from msg.core.codec import b64
    first=source.read_bytes()[:89]
    await client.call('transfer.part_put',{'transfer_id':tid,'offset':0,'data':b64(first),'digest':digest(first)})
    resumed=MsgClient(state,PathGETTransport(app.settings.service_url,http=http),clock=lambda:NOW)
    sealed=await resumed.upload(source,transfer_id=tid,part_bytes=89)
    assert sealed.status=='ok'
    target=tmp_path/'download.bin'
    await client.download(sealed.output,target,part_bytes=83)
    assert target.read_bytes()==source.read_bytes()
    await http.aclose()


@pytest.mark.asyncio
async def test_client_retry_reuses_request_and_does_not_replace_local_identity(installed,tmp_path):
    app,_=installed
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),base_url=app.settings.service_url)
    state=ClientState(tmp_path/'client',server=app.settings.service_url)
    client=MsgClient(state,HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
    await client.register('retry-agent')
    first=state.signer.private_bytes()
    with pytest.raises(Failure,match='identity_already_configured'):
        await client.register('another-agent')
    assert state.signer.private_bytes()==first
    old_identity=state.age_key_path.read_text()
    rotated=await client.rotate_encryption_key()
    assert rotated.status=='ok' and state.encryption_recipient==rotated.data['recipient']
    historical=state.directory/('encryption-'+rotated.data['previous_key_id']+'.agekey')
    assert historical.read_text()==old_identity
    assert state.age_key_path.read_text()!=old_identity
    packet=client.prepare('content.post_create',{'parent':'/main','body':'retry'},request_id='client-stable-id')
    one=await client.send(packet)
    two=await client.send(packet)
    assert one.resources==two.resources and two.replayed
    await http.aclose()

@pytest.mark.asyncio
async def test_uploaded_markdown_can_be_published_without_copying_body(installed,tmp_path):
    app,_=installed
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),base_url=app.settings.service_url)
    state=ClientState(tmp_path/'publisher',server=app.settings.service_url)
    client=MsgClient(state,HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
    await client.register('source-publisher')
    source=tmp_path/'note.md';source.write_bytes('文本\r\n原件\n'.encode())
    uploaded=await client.upload(source,media_type='text/markdown')
    from msg.core.codec import wire
    posted=await client.call('content.post_create',{'parent':'/main','source':wire(uploaded.output)})
    assert posted.status=='ok',posted
    raw=await http.get('/_id/'+posted.resources[0].id+'/raw')
    assert raw.content==source.read_bytes()
    await http.aclose()
