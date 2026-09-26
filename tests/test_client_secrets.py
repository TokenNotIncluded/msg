import httpx
import pytest
from msg.client import ClientState,MsgClient
from msg.client_secrets import encryption_keygen,put_secret,get_secret
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from test_service import NOW

@pytest.mark.asyncio
async def test_client_encrypts_before_network_and_decrypts_only_locally(installed,tmp_path):
    app,_=installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),base_url=app.settings.service_url) as http:
        client=MsgClient(ClientState(tmp_path/'client',server=app.settings.service_url),HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
        await client.register('secret-client')
        pair=encryption_keygen(tmp_path/'enc.key')
        source=tmp_path/'private.txt';source.write_bytes(b'private-api-key-local-only')
        result=client.checked(await put_secret(client,'opaque-id',source,pair['recipient']))
        output=tmp_path/'recovered.txt'
        await get_secret(client,result.resources[0].id,output,tmp_path/'enc.key')
        assert output.read_bytes()==source.read_bytes()
        assert output.stat().st_mode&0o777==0o600
        async with app.metadata.transaction(write=False) as tx:
            from msg.core.models import ResourceRef
            revision=await tx.revision(result.resources[0])
        ciphertext=await app.contents.read_bytes(revision.content,limit=1048576)
        assert source.read_bytes() not in ciphertext
        renewed=await client.renew_certificate()
        assert renewed.status=='ok'
        assert client.state.certificates==(renewed.data['certificate_id'],)
