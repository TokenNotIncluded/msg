"""Spelling hints are explicit, deterministic and limited to visible names."""
import httpx
import pytest
from test_lexical_search_grep import business_state
from test_service import call, register

from msg.core.codec import wire
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_spelling_is_opt_in_and_rechecks_visible_name_dictionary(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'spelling-owner')
    posts = []
    for name in ('gadget', 'gadger'):
        created = await call(app, 'content.post_create',
            {'parent': '/main', 'name': name, 'body': 'a product'}, key=key, subject=subject)
        assert created.status == 'ok', wire(created)
        posts.append(created)
    private = await call(app, 'content.chmod', {'id': posts[1].resources[0].id, 'mode': '0600'},
        key=key, subject=subject, expected=((posts[1].resources[0].id, posts[1].data['generation']),))
    assert private.status == 'ok', wire(private)
    args = {'scope': '/main', 'terms': 'gadge', 'field': 'name'}
    before = await business_state(app)
    ordinary = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert ordinary.status == 'ok' and 'spelling' not in ordinary.data
    hinted = await call(app, 'discovery.lexical_search', {**args, 'spell': True}, contract_version=5)
    assert hinted.status == 'ok', wire(hinted)
    assert wire(hinted.data['spelling']) == {'source': 'name', 'terms': [
        {'term': 'gadge', 'suggestions': [{'value': 'gadget', 'distance': 1, 'count': 1}]}]}
    assert [row['id'] for row in hinted.data['items']] == [row['id'] for row in ordinary.data['items']]
    old = await call(app, 'discovery.lexical_search', {**args, 'spell': True}, contract_version=4)
    assert old.status == 'error'
    short = await call(app, 'discovery.lexical_search', {**args, 'terms': 'g', 'spell': True}, contract_version=5)
    assert short.status == 'error' and short.error.code == 'query_cost_exceeded'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        query = await http.get('/_search', params={**args, 'spell': '1'})
        path = await http.get('/_s/q/5/s/%2Fmain/t/gadge/f/n/sp/1')
        assert query.status_code == path.status_code == 200, (query.text, path.text)
        assert query.content == path.content
    assert await business_state(app) == before
    revoked = await call(app, 'content.chmod', {'id': posts[0].resources[0].id, 'mode': '0600'},
        key=key, subject=subject, expected=((posts[0].resources[0].id, posts[0].data['generation']),))
    assert revoked.status == 'ok', wire(revoked)
    hidden = await call(app, 'discovery.lexical_search', {**args, 'spell': True}, contract_version=5)
    assert hidden.status == 'ok' and not hidden.data['items']
    assert wire(hidden.data['spelling']) == {'source': 'name', 'terms': [{'term': 'gadge', 'suggestions': []}]}


@pytest.mark.asyncio
async def test_v5_registered_collaboration_relation_names_select_same_http_contract(installed):
    app, _ = installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        for kind in ('reference', 'state', 'target', 'content'):
            query = await http.get('/_search', params={
                'scope': '/main', 'terms': 'relationneedle', 'relation_type': kind})
            path = await http.get('/_s/q/5/s/%2Fmain/t/relationneedle/rt/' + kind)
            assert query.status_code == path.status_code == 200, (query.text, path.text)
            assert query.content == path.content
            old = await http.get('/_s/q/4/s/%2Fmain/t/relationneedle/rt/' + kind)
            assert old.status_code >= 400
