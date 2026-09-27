"""Search suggestions are a bounded projection of current readable matches."""
from datetime import timedelta

import httpx
import pytest

from msg.core.codec import b64,canonical,digest,wire
from msg.core.requests import request_for
from msg.transports.http import create_app
from test_service import NOW,call,register


@pytest.mark.asyncio
async def test_v4_http_and_query_ref_use_same_authorized_suggestions(installed):
    app,_=installed
    key,user,_=await register(app,'suggest-http-owner')
    created=await call(app,'content.file_put',{'parent':'/main',
        'name':'suggestpath-one.txt','data':b64(b'body'),'media_type':'text/plain'},
        key=key,subject=user)
    assert created.status=='ok'
    query='/_search?scope=%2Fmain&terms=suggestpath&field=name&suggest=1'
    path='/_s/q/4/s/%2Fmain/t/suggestpath/f/n/sg/1'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        left=await http.get(query)
        right=await http.get(path)
        assert left.status_code==right.status_code==200,(left.text,right.text)
        assert left.content==right.content
        assert left.json()['suggestions']==[{'value':'suggestpath-one','count':1}]
    args={'scope':'/main','terms':'suggestpath','field':'name','suggest':True}
    descriptor=canonical({'version':1,'kind':'search','arguments':args})
    opened=await call(app,'transfer.open',{'direction':'upload','size':len(descriptor),
        'digest':digest(descriptor),'media_type':'application/vnd.msg.read-query+json'},
        key=key,subject=user)
    tid=opened.data['transfer_id']
    await call(app,'transfer.part_put',{'transfer_id':tid,'offset':0,
        'data':b64(descriptor),'digest':digest(descriptor)},key=key,subject=user)
    await call(app,'transfer.seal',{'transfer_id':tid,'final_size':len(descriptor),
        'final_digest':digest(descriptor)},key=key,subject=user)
    sealed=await call(app,'transfer.query_seal',{'transfer_id':tid},key=key,subject=user)
    assert sealed.status=='ok',wire(sealed)
    token=sealed.data['query_ref']
    proof=request_for('transfer.query_get',{'query_ref':token},app.settings.service_url,
                      signer=key,subject=user,expires_at=NOW+timedelta(seconds=60))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        resolved=await http.get('/_r/q/'+token+'/p/'+b64(canonical(proof)))
        assert resolved.status_code==200,resolved.text
        assert resolved.json()['suggestions']==left.json()['suggestions']


@pytest.mark.asyncio
async def test_v4_suggestions_count_only_matching_readable_names_and_recheck_page(installed):
    app,_=installed
    key,user,_=await register(app,'suggest-owner')
    created=[]
    for name in ('suggestible-one.txt','suggestible-two.txt','suggestible-secret.txt'):
        result=await call(app,'content.file_put',{'parent':'/main','name':name,
            'data':b64(b'body'),'media_type':'text/plain'},key=key,subject=user)
        assert result.status=='ok',wire(result)
        created.append(result)
    hidden=created[2]
    locked=await call(app,'content.chmod',{'id':hidden.resources[0].id,'mode':'0600'},
                      key=key,subject=user,
                      expected=((hidden.resources[0].id,hidden.data['generation']),))
    assert locked.status=='ok',wire(locked)
    args={'scope':'/main','terms':'suggest','field':'name','limit':1,'suggest':True}
    first=await call(app,'discovery.lexical_search',args,contract_version=4)
    assert first.status=='ok',wire(first)
    assert wire(first)['data']['suggestions']==[
        {'value':'suggestible-one','count':1},
        {'value':'suggestible-two','count':1}]
    assert first.data['cursor']
    old=await call(app,'discovery.lexical_search',{'cursor':first.data['cursor']},
                   contract_version=3)
    assert old.status=='error' and old.error.code=='cursor_query_mismatch'
    first_id=first.data['items'][0]['id']
    newly_hidden=next(item for item in created[:2]
                      if item.resources[0].id!=first_id)
    locked=await call(app,'content.chmod',{'id':newly_hidden.resources[0].id,'mode':'0600'},
                      key=key,subject=user,
                      expected=((newly_hidden.resources[0].id,newly_hidden.data['generation']),))
    assert locked.status=='ok',wire(locked)
    continued=await call(app,'discovery.lexical_search',
                         {'cursor':first.data['cursor']},contract_version=4)
    assert continued.status=='ok',wire(continued)
    expected_word=next(name[:-4] for name,item in zip(
        ('suggestible-one.txt','suggestible-two.txt'),created[:2])
        if item.resources[0].id==first_id)
    assert wire(continued)['data']['suggestions']==[{'value':expected_word,'count':1}]
    assert list(continued.data['items'])==[]
    owner=await call(app,'discovery.lexical_search',args,key=key,subject=user,
                     contract_version=4)
    assert owner.status=='ok',wire(owner)
    assert len(owner.data['suggestions'])==3


@pytest.mark.asyncio
async def test_v4_suggest_is_explicit_and_old_schemas_are_stable(installed):
    app,_=installed
    args={'scope':'/main','terms':'sug','field':'name'}
    for version in (1,2,3):
        result=await call(app,'discovery.lexical_search',
                          {**args,'suggest':True},contract_version=version)
        assert result.status=='error',wire(result)
    result=await call(app,'discovery.lexical_search',args,contract_version=4)
    assert result.status=='ok',wire(result)
    assert 'suggestions' not in result.data
    too_short=await call(app,'discovery.lexical_search',
                         {**args,'terms':'s','suggest':True},contract_version=4)
    assert too_short.status=='error' and too_short.error.code=='query_cost_exceeded'
