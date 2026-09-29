"""Bounded lexical discovery and scoped grep never reveal unreadable content."""
from datetime import timedelta
from dataclasses import replace

import httpx
import pytest

from msg.core.codec import b64, canonical, digest, wire
from msg.core.requests import request_for
from msg.transports.http import create_app
from test_service import NOW, call, register


async def business_state(app):
    async with app.metadata.transaction(write=False) as tx:
        return (tx.one('SELECT COUNT(*) FROM events')[0],
                tx.one('SELECT COUNT(*) FROM jobs')[0],
                tx.one('SELECT COUNT(*) FROM reactions')[0],
                tx.one('SELECT COUNT(*) FROM messages')[0],
                tx.one('SELECT COUNT(*) FROM revisions')[0],
                tx.one('SELECT SUM(generation) FROM resources')[0])


@pytest.mark.asyncio
async def test_lexical_search_query_string_and_v2_path_match_rank_and_cursor(installed):
    app, _ = installed
    key,user,_=await register(app,'lexical-owner')
    first=await call(app,'content.post_create',
                     {'parent':'/main','body':'Alpha beta exact phrase. Alpha again.'},
                     key=key,subject=user)
    second=await call(app,'content.post_create',
                      {'parent':'/main','body':'Alpha beta as two terms.'},
                      key=key,subject=user)
    args={'scope':'/main','terms':'alpha beta','mode':'all','field':'body',
          'order':'relevance','limit':1,'snippet':True,'explain':'compact'}
    before=await business_state(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        legacy=await http.get('/_search?query=Alpha')
        assert legacy.status_code==200
        query=await http.get('/_search?scope=%2Fmain&terms=alpha%20beta&mode=all&field=body'
                             '&order=relevance&limit=1&snippet=1&explain=compact')
        path=await http.get('/_s/q/2/s/%2Fmain/t/alpha%20beta/m/a/f/b/o/r/n/1/x/c/e/c')
        long=await http.get('/_search/q/2/s/%2Fmain/t/alpha%20beta/m/a/f/b/o/r/n/1/x/c/e/c')
        assert query.status_code==path.status_code==long.status_code==200,(query.text,path.text)
        assert app.cursors.inspect(query.json()['cursor'])==app.cursors.inspect(path.json()['cursor'])
        assert query.content==path.content==long.content
        assert query.headers['etag']==path.headers['etag']==long.headers['etag']
        item=query.json()['items'][0]
        assert item['ref']['id'] in {first.resources[0].id,second.resources[0].id}
        assert item['snippet']['field']=='body' and len(item['snippet']['text'])<160
        assert item['rank_reason']['matched_fields']==['body']
        assert query.json()['next'].startswith('/_r/c/')
        continued=await http.get(query.json()['next'])
        assert continued.status_code==200
        assert {item['ref']['id'] for item in query.json()['items']+continued.json()['items']}=={
            first.resources[0].id,second.resources[0].id}
    assert await business_state(app)==before


@pytest.mark.asyncio
async def test_private_snippet_and_count_are_filtered_before_output(installed):
    app, _ = installed
    key,user,_=await register(app,'private-search-owner')
    created=await call(app,'content.post_create',
                       {'parent':'/main','body':'uniquesecretphrase private body'},
                       key=key,subject=user)
    rid=created.resources[0].id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        before=await http.get('/_s/q/2/s/%2Fmain/t/uniquesecretphrase/f/b/x/1')
        assert before.status_code==200
        assert [item['ref']['id'] for item in before.json()['items']]==[rid]
    locked=await call(app,'content.chmod',{'id':rid,'mode':'0600'},
                      key=key,subject=user,expected=((rid,created.data['generation']),))
    assert locked.status=='ok'
    args={'scope':'/main','terms':'uniquesecretphrase','field':'body','snippet':True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        hidden=await http.get('/_s/q/2/s/%2Fmain/t/uniquesecretphrase/f/b/x/1')
        assert hidden.status_code==200
        assert hidden.json()['items']==[]
        assert 'total' not in hidden.json() and rid not in hidden.text
        packet=request_for('discovery.lexical_search',args,app.settings.service_url,
                           signer=key,subject=user,expires_at=NOW+timedelta(seconds=120))
        signed=await http.get('/_search?scope=%2Fmain&terms=uniquesecretphrase&field=body&snippet=1',
                              headers={'X-Msg-Request':b64(canonical(packet))})
        assert signed.status_code==200,signed.text
        assert signed.json()['items'][0]['ref']['id']==rid
        assert 'uniquesecretphrase' in signed.json()['items'][0]['snippet']['text']


@pytest.mark.asyncio
async def test_facets_use_all_currently_readable_matches_and_recheck_old_cursor(installed):
    app, _ = installed
    key,user,_=await register(app,'facet-owner')
    created=[]
    for words,tag in [('facetneedle '*4,'shared'),('facetneedle '*3,'shared'),
                      ('facetneedle','private-only')]:
        post=await call(app,'content.post_create',{'parent':'/main','body':words},
                        key=key,subject=user)
        rid=post.resources[0].id
        tagged=await call(app,'content.tags_set',{'id':rid,'tags':[tag]},
                          key=key,subject=user,expected=((rid,post.data['generation']),))
        assert tagged.status=='ok',wire(tagged)
        created.append((rid,tagged.data['generation']))
    args={'scope':'/main','terms':'facetneedle','field':'body','limit':1,
          'facets':['type','tag']}
    before=await business_state(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        # QueryRef and ordinary reads use the same lexical operation. The HTTP
        # parameter parser is tested separately once it exposes `facets`.
        first=await call(app,'discovery.lexical_search',args,contract_version=2)
        assert first.status=='ok',wire(first)
        first_data=wire(first)['data']
        assert first_data['facets']=={
            'type':[{'value':'post','count':3}],
            'tag':[{'value':'shared','count':2},{'value':'private-only','count':1}]}
        assert first_data['items'][0]['ref']['id']==created[0][0]
        assert first_data['next'].startswith('/_r/c/')
        hidden=created[2]
        locked=await call(app,'content.chmod',{'id':hidden[0],'mode':'0600'},
                          key=key,subject=user,expected=(hidden,))
        assert locked.status=='ok',wire(locked)
        after_write=await business_state(app)
        continued=await http.get(first_data['next'])
        assert continued.status_code==200,continued.text
        body=continued.json()
        assert [item['ref']['id'] for item in body['items']]==[created[1][0]]
        assert body['facets']=={'type':[{'value':'post','count':2}],
                                'tag':[{'value':'shared','count':2}]}
        assert hidden[0] not in continued.text and 'private-only' not in continued.text
        fresh=await call(app,'discovery.lexical_search',args,contract_version=2)
        assert fresh.status=='ok' and wire(fresh)['data']['facets']==body['facets']
    assert await business_state(app)==after_write
    assert before!=after_write


@pytest.mark.asyncio
async def test_v2_path_filter_segments_match_query_string_and_are_described(installed):
    app, _ = installed
    key,user,_=await register(app,'lexical-filter-owner')
    created=await call(app,'content.post_create',
                       {'parent':'/main','body':'filterneedle body'},key=key,subject=user)
    query=(f'/_search?scope=%2Fmain&terms=filterneedle&field=body&type=post&owner={user}'
           '&has_attachment=0&depth=1&recursive=1&fields=id,name&limit=2')
    path=(f'/_s/q/2/s/%2Fmain/t/filterneedle/f/b/y/post/w/{user}'
          '/ha/0/d/1/re/1/fi/id,name/n/2')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        left=await http.get(query)
        right=await http.get(path)
        assert left.status_code==right.status_code==200,(left.text,right.text)
        assert left.content==right.content
        assert [item['id'] for item in left.json()['items']]==[created.resources[0].id]
        schema=await http.get('/-/d/search.query')
        assert schema.status_code==200
        assert schema.json()['segments']['has_attachment']=='ha'


@pytest.mark.asyncio
async def test_known_scope_grep_fixed_regex_glob_and_cost_limits(installed):
    app, _ = installed
    key,user,_=await register(app,'grep-owner')
    visible=await call(app,'content.post_create',
                       {'parent':'/main','body':'first line\nNeedle value\nlast line\n'},
                       key=key,subject=user)
    private=await call(app,'content.post_create',
                       {'parent':'/main','body':'Needle secret only'},key=key,subject=user)
    await call(app,'content.chmod',{'id':private.resources[0].id,'mode':'0600'},
               key=key,subject=user,
               expected=((private.resources[0].id,private.data['generation']),))
    before=await business_state(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        found=await http.get('/_search/grep?scope=%2Fmain&pattern=Needle&glob=*.md'
                             '&before=1&after=1&max_matches=10')
        assert found.status_code==200,found.text
        pure=await http.get('/_s/g/1/s/%2Fmain/t/Needle/g/%2A.md/b/1/a/1/m/10')
        assert pure.status_code==200,pure.text
        assert pure.content==found.content and pure.headers['etag']==found.headers['etag']
        assert [m['ref']['id'] for m in found.json()['matches']]==[visible.resources[0].id]
        match=found.json()['matches'][0]
        assert match['line_hint']==2 and match['range']==[0,6]
        assert 'Needle value' in match['context']
        assert private.resources[0].id not in found.text
        regex=await http.get('/_search/grep?scope=%2Fmain&pattern=N.edle&regex=1')
        assert regex.status_code==200
        counted=await http.get('/_search/grep?scope=%2Fmain&pattern=Needle&count_only=1')
        assert counted.json()['count']==1
        unsafe=await http.get('/_search/grep?scope=%2Fmain&pattern=(a+)+$&regex=1')
        assert unsafe.status_code==400
        assert unsafe.json()['error']['code']=='invalid_grep_pattern'
        unknown=await http.get('/_search/grep?pattern=Needle')
        assert unknown.status_code==400
    assert await business_state(app)==before


@pytest.mark.asyncio
async def test_grep_reports_each_match_on_the_same_authorized_line(installed):
    app, _ = installed
    key,user,_=await register(app,'grep-multiple')
    post=await call(app,'content.post_create',
                    {'parent':'/main','body':'Needle then Needle again\nİ needle\n'},
                    key=key,subject=user)
    assert post.status=='ok'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        matches=await http.get('/_s/grep?scope=%2Fmain&pattern=Needle')
        assert matches.status_code==200
        own=[item for item in matches.json()['matches'] if item['ref']['id']==post.resources[0].id]
        assert [item['range'] for item in own]==[[0,6],[12,18]]
        folded=await http.get('/_s/grep?scope=%2Fmain&pattern=needle&case_sensitive=0')
        own_folded=[item for item in folded.json()['matches']
                    if item['ref']['id']==post.resources[0].id]
        assert [item['range'] for item in own_folded]==[[0,6],[12,18],[2,8]]


@pytest.mark.asyncio
async def test_unrelated_resources_do_not_exhaust_search_or_grep_budget(installed):
    app, _ = installed
    key,user,_=await register(app,'scoped-budget-owner')
    target=await call(app,'content.post_create',
                      {'parent':'/main','body':'scopedbudgetneedle'},key=key,subject=user)
    # These rows sort before ordinary generated IDs but are outside /main.
    # A global LIMIT or pre-scope scan counter would hide the real hit.
    async with app.metadata.transaction(write=True) as tx:
        sample=await tx.resource(target.resources[0].id)
        for index in range(2001):
            await tx.insert(replace(sample,id=f'0-budget-{index:04d}',
                                    parent=user,name=f'budget-{index:04d}',revision=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        searched=await http.get('/_s/q/2/s/%2Fmain/t/scopedbudgetneedle/f/b')
        grepped=await http.get('/_s/grep?scope=%2Fmain&pattern=scopedbudgetneedle')
        assert searched.status_code==grepped.status_code==200,(searched.text,grepped.text)
        assert [item['ref']['id'] for item in searched.json()['items']]==[sample.id]
        assert [item['ref']['id'] for item in grepped.json()['matches']]==[sample.id]


@pytest.mark.asyncio
async def test_sealed_search_query_ref_reads_same_authorized_results(installed):
    app, _ = installed
    key,user,_=await register(app,'queryref-search')
    result=await call(app,'content.post_create',
                      {'parent':'/main','body':'queryref lexical needle'},key=key,subject=user)
    second=await call(app,'content.post_create',
                      {'parent':'/main','body':'queryref lexical second'},key=key,subject=user)
    args={'scope':'/main','terms':'queryref lexical','mode':'all','field':'body',
          'limit':1,'facets':['type']}
    descriptor=canonical({'version':1,'kind':'search','arguments':args})
    opened=await call(app,'transfer.open',{'direction':'upload','size':len(descriptor),
        'digest':digest(descriptor),'media_type':'application/vnd.msg.read-query+json'},
        key=key,subject=user)
    tid=opened.data['transfer_id']
    await call(app,'transfer.part_put',{'transfer_id':tid,'offset':0,
        'data':b64(descriptor),'digest':digest(descriptor)},key=key,subject=user)
    await call(app,'transfer.seal',{'transfer_id':tid,
        'final_size':len(descriptor),'final_digest':digest(descriptor)},key=key,subject=user)
    sealed=await call(app,'transfer.query_seal',{'transfer_id':tid},key=key,subject=user)
    assert sealed.status=='ok',wire(sealed)
    token=sealed.data['query_ref']
    before=await business_state(app)
    packet=request_for('transfer.query_get',{'query_ref':token},app.settings.service_url,
                       signer=key,subject=user,expires_at=NOW+timedelta(seconds=60))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        path=await http.get('/_r/q/'+token+'/p/'+b64(canonical(packet)))
        assert path.status_code==200,path.text
        assert len(path.json()['items'])==1
        assert path.json()['facets']=={'type':[{'value':'post','count':2}]}
        assert path.json()['next'].startswith('/_r/q/'+token+'/c/')
        page_packet=request_for('transfer.query_get',
            {'query_ref':token,'cursor':path.json()['cursor']},app.settings.service_url,
            signer=key,subject=user,expires_at=NOW+timedelta(seconds=60))
        continued=await http.get(path.json()['next']+'/p/'+b64(canonical(page_packet)))
        assert continued.status_code==200,continued.text
        assert continued.json()['facets']==path.json()['facets']
        assert {item['ref']['id'] for item in path.json()['items']+continued.json()['items']}=={
            result.resources[0].id,second.resources[0].id}
    assert await business_state(app)==before


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix,matched,query', [
    ('ß' * 80, 'Target', 'target'),
    ('İ' * 80, 'Straße', 'STRASSE'),
    ('x' * 80, 'ß', 's'),
])
async def test_unicode_casefold_snippet_ranges_address_original_text(installed, prefix, matched, query):
    app, _ = installed
    key, subject, _ = await register(app, 'unicode-search-owner')
    created = await call(app, 'content.post_create',
                         {'parent': '/main', 'body': prefix + matched + '尾' * 80},
                         key=key, subject=subject)
    assert created.status == 'ok', wire(created)
    result = await call(app, 'discovery.lexical_search', {
        'scope': '/main', 'terms': query, 'field': 'body', 'snippet': True,
    }, key=key, subject=subject)
    assert result.status == 'ok', wire(result)
    item = next(item for item in result.data['items'] if item['id'] == created.resources[0].id)
    snippet = item['snippet']
    start, end = snippet['range']
    assert snippet['text'][start:end] == matched
    assert snippet['text'] == prefix[-40:] + matched + '尾' * 40
