from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import pytest
import httpx

from msg.core.codec import b64,canonical,wire,decode
from msg.core.errors import Failure
from msg.core.models import Credential
from msg.core.requests import request_for
from msg.transports.http import create_app
from msg.transports.client import HTTPTransport,PathGETTransport,MCPHTTPTransport
from msg.transports.dictionary import build_dictionary,read_query_path_document
from msg.transports.read_tree_path import decode_read_tree_path
from .test_read_query_tree import tree,query


PATH='/_r/q/3/r/r_tree/n/1/f/i/x/children/n/1/f/i/x/children/n/1/f/i/up/1/up/1'


async def business_state(app):
    async with app.metadata.transaction(write=False) as tx:
        names=[row[0] for row in tx.rows("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name:sorted(tx.rows('SELECT * FROM "'+name.replace('"','""')+'"'),key=repr) for name in names}


def test_path_tree_is_readable_and_round_trips_without_json_payload():
    args,proof=decode_read_tree_path(PATH.encode())
    assert args=={**query(),'query_version':3}
    assert proof is None
    aliased,suffix=decode_read_tree_path(PATH.replace('/_r/','/_read/') .encode()+b'/p/signed')
    assert aliased==args and suffix==b'signed'


@pytest.mark.parametrize('tail',[
    '/r/r_tree/r/other','/r/r_tree/x/children','/r/r_tree/up/1',
    '/r/r_tree/x/unknown/up/1','/r/r_tree/x/children/n/11/up/1',
    '/r/r_tree/x/children/r/other/up/1','/r/r_tree/f/i,i',
    '/r/r_tree/f/secret','/r/%72_tree','/r/%FF','/r/bad%escape',
    '/r/r_tree/x/children/up/1/x/children/up/1','/a/cursor/r/r_tree',
])
def test_path_rejects_ambiguity_and_noncanonical_encodings(tail):
    with pytest.raises(Failure):decode_read_tree_path(('/_r/q/3'+tail).encode())


async def test_http_alias_header_path_head_etag_revoke_and_replay_window(harness):
    h=harness;await tree(h)
    # A private collection needs an actual credential; URL possession alone is insufficient.
    async with h.app.metadata.transaction(write=True) as tx:
        resource=await tx.resource('r_tree')
        await tx.replace(replace(resource,mode=0o700,generation=resource.generation+1),resource.generation)
    args,_=decode_read_tree_path(PATH.encode())
    packet=h.packet('discovery.read_query',args,3)
    proof=b64(canonical(packet));headers={'X-Msg-Request':proof}
    before=await business_state(h.app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(h.app)),base_url=h.app.settings.service_url) as http:
        anonymous=await http.get(PATH)
        assert anonymous.status_code==403,anonymous.text
        plain=await http.get(PATH,headers=headers)
        signed=await http.get(PATH+'/p/'+proof)
        alias=await http.get(PATH.replace('/_r/','/_read/')+'/p/'+proof)
        replay=await http.get(PATH+'/p/'+proof)
        assert plain.status_code==signed.status_code==alias.status_code==replay.status_code==200,(plain.text,signed.text)
        assert plain.content==signed.content==alias.content==replay.content
        assert signed.headers['cache-control']=='no-store'
        head=await http.head(PATH,headers=headers)
        cached=await http.get(PATH,headers={**headers,'If-None-Match':signed.headers['etag']})
        assert head.status_code==200 and head.content==b''
        assert cached.status_code==304
        assert head.headers['etag']==alias.headers['etag']==signed.headers['etag']
        assert await business_state(h.app)==before
        # A short-lived signed URL is replayable inside its explicitly bounded
        # window. Replaying it cannot write a checkpoint/ACK/credential/event.
        expired=request_for('discovery.read_query',args,h.app.settings.service_url,
            subject='u_alice',signer=h.signer,contract_version=3,
            expires_at=h.ctx.now-timedelta(seconds=1))
        stale=await http.get(PATH+'/p/'+b64(canonical(expired)))
        assert stale.status_code==400 and stale.json()['error']['code']=='path_proof_expiry'
        assert await business_state(h.app)==before
        async with h.app.metadata.transaction(write=True) as tx:
            resource=await tx.resource('r_tree')
            await tx.replace(replace(resource,owner='u_other',mode=0,generation=resource.generation+1),resource.generation)
        after=await business_state(h.app)
        denied=await http.get(PATH,headers={**headers,'If-None-Match':signed.headers['etag']})
        assert denied.status_code==403,denied.text
        assert await business_state(h.app)==after


@pytest.mark.parametrize('transport_class',[HTTPTransport,PathGETTransport,MCPHTTPTransport])
async def test_real_client_transports_share_executor_projection_and_revocation(harness,transport_class):
    h=harness;await tree(h)
    packet=h.packet('discovery.read_query',query(),3)
    before=await business_state(h.app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(h.app)),base_url=h.app.settings.service_url) as http:
        client=transport_class(h.app.settings.service_url,http=http)
        actual=await client.call(packet)
        expected=await h.app.executor.execute(packet)
        assert actual.error is None,wire(actual)
        assert canonical(actual)==canonical(expected)
        assert len(actual.data['items'])==1
        nested=actual.data['items'][0]['collections']['children']
        cursor=nested['pageInfo']['endCursor']
        assert nested['next']=='/_r/c/'+cursor
        continued=await client.call(h.packet('discovery.read_query',{'cursor':cursor},3))
        assert continued.error is None,wire(continued)
        assert continued.data['items'][0]['id']=='r_ab'
        assert await business_state(h.app)==before
        async with h.app.metadata.transaction(write=True) as tx:
            credential=await tx.credential(h.signer.key_id)
            tx.execute('UPDATE credentials SET body=? WHERE id=?',
                (canonical(replace(credential,revoked_at=h.ctx.now)).decode(),credential.id),write=True)
        after=await business_state(h.app)
        denied=await client.call(packet)
        assert denied.error.code=='credential_revoked'
        assert await business_state(h.app)==after


async def test_read_version_cannot_be_replaced_by_proof(harness):
    h=harness;await tree(h)
    args,_=decode_read_tree_path(PATH.encode())
    wrong=b64(canonical(h.packet('discovery.read_query',args,2)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(h.app)),base_url=h.app.settings.service_url) as http:
        for headers,url in (({'X-Msg-Request':wrong},PATH),({},PATH+'/p/'+wrong)):
            response=await http.get(url,headers=headers)
            assert response.status_code==400
            assert response.json()['error']['code']=='representation_mismatch'


async def test_path_length_and_token_only_url_reject_without_echo(harness):
    h=harness;await tree(h)
    args,_=decode_read_tree_path(PATH.encode())
    secret=b'test-only-replayable-token-do-not-echo'
    async with h.app.metadata.transaction(write=True) as tx:
        key=await tx.credential(h.signer.key_id)
        credential=replace(key,id='tok_test',kind='token',verifier=sha256(secret).digest())
        tx.execute('INSERT INTO credentials VALUES (?,?,?)',
            (credential.id,credential.subject_id,canonical(credential).decode()),write=True)
    packet=request_for('discovery.read_query',args,h.app.settings.service_url,
        subject='u_alice',token=('tok_test',secret),contract_version=3,
        expires_at=h.ctx.now+timedelta(seconds=30))
    encoded=b64(canonical(packet))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(h.app)),base_url=h.app.settings.service_url) as http:
        allowed=await http.get(PATH,headers={'X-Msg-Request':encoded})
        assert allowed.status_code==200,allowed.text
        denied=await http.get(PATH+'/p/'+encoded)
        assert denied.status_code==400
        assert denied.json()['error']['code']=='path_signature_required'
        assert secret.decode() not in denied.text and b64(secret) not in denied.text
        too_long=await http.get('/_r/q/3/r/'+'x'*8200)
        assert too_long.status_code==413
        assert 'x'*100 not in too_long.text


async def test_v3_dictionary_preserves_published_codes(harness):
    from importlib.resources import files
    from msg.core.codec import loads
    h=harness
    document=read_query_path_document(h.app.registry)
    assert document['available_versions']==[1,2,3]
    assert document['segments_v3']['enter']=='x'
    dictionary=build_dictionary(h.app.registry)
    published=loads(files('msg.data').joinpath('shortcodes.json').read_bytes())
    for kind,rows in published['codes'].items():
        for row in rows:
            current=next(item for item in dictionary.document['codes'][kind] if item['code']==row['code'])
            assert current['identity']==row['identity']
            if current['deprecated']:
                with pytest.raises(Failure) as exc:dictionary.resolve(kind,row['code'])
                assert exc.value.code=='deprecated_short_code'
            else:
                assert dictionary.resolve(kind,row['code'])['identity']==row['identity']
    assert dictionary.code_for('operation','discovery.read_query@3')
