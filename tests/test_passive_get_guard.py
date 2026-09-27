"""GET execution URLs cannot be triggered by passive clients."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from msg.core.codec import b64, canonical
from msg.application import Application
from msg.core.requests import request_for
from msg.transports.dictionary import build_dictionary
from msg.transports.http import RouteEffect, classify_route, create_app
from test_service import NOW, register


@pytest.fixture(scope='module')
def registry():
    return Application(SimpleNamespace(server=SimpleNamespace(plugins=(
        'identity','content','discussion','communication','discovery','transfer','extensions')))).registry


def execution_path(app,key,user,request_id,body='SECRET_MARKER'):
    packet=request_for('content.post_create',{'parent':'/main','body':body},
                       app.settings.service_url,signer=key,subject=user,
                       request_id=request_id,expires_at=NOW+timedelta(seconds=120))
    return '/-/g/content.post_create/j/'+b64(canonical(packet)),packet


async def post_count(app):
    async with app.metadata.transaction(write=False) as tx:
        return (tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0],
                tx.one('SELECT COUNT(*) FROM events')[0],
                tx.one('SELECT COUNT(*) FROM jobs')[0])


@pytest.mark.parametrize('path,method,effect',(
    ('/main','GET',RouteEffect.PURE_READ),
    ('/_read/query','GET',RouteEffect.PURE_READ),
    ('/-/d','GET',RouteEffect.PURE_READ),
    ('/-/g/discovery.get/j/example','GET',RouteEffect.PURE_READ),
    ('/-/g/content.post_create/j/example','GET',RouteEffect.BUSINESS_WRITE),
    ('/-/g/tool.run/j/example','GET',RouteEffect.EXTERNAL_EFFECT),
    ('/-/p/content.post_create','POST',RouteEffect.BUSINESS_WRITE),
))
def test_route_effect_classification(registry,path,method,effect):
    assert classify_route(path,method,registry).effect is effect


@pytest.mark.asyncio
async def test_passive_clients_cannot_execute_even_fully_signed_get_url(installed):
    app, _ = installed
    key,user,_=await register(app,'passive-guard')
    before=await post_count(app)
    passive_headers=(
        {'User-Agent':'Googlebot/2.1'},
        {'User-Agent':'Slackbot-LinkExpanding 1.0'},
        {'User-Agent':'security-scanner/1.0'},
        {'User-Agent':'Mozilla/5.0 Chrome/140.0 Safari/537.36'},
        {'User-Agent':'AgentRuntime/1.0','Purpose':'prefetch'},
        {'User-Agent':'AgentRuntime/1.0','Sec-Purpose':'prerender'},
        {'User-Agent':'AgentRuntime/1.0','X-Moz':'prefetch'},
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for index,headers in enumerate(passive_headers):
            url,_=execution_path(app,key,user,f'passive-{index}')
            blocked=await http.get(url,headers=headers)
            assert blocked.status_code==403,blocked.text
            assert blocked.json()['error']['code']=='passive_client_forbidden'
            assert 'no-store' in blocked.headers['cache-control']
            robots=blocked.headers['x-robots-tag'].lower()
            assert 'noindex' in robots and 'nofollow' in robots
            assert 'SECRET_MARKER' not in blocked.text
            assert await post_count(app)==before
        ordinary=await http.get('/main?operation=content.post_create&body=SECRET_MARKER',
                                headers={'User-Agent':'Googlebot'})
        assert ordinary.status_code==200
        assert await post_count(app)==before


@pytest.mark.asyncio
async def test_agent_get_still_requires_bound_proof_and_idempotency(installed):
    app, _ = installed
    key,user,_=await register(app,'agent-get-guard')
    url,packet=execution_path(app,key,user,'agent-once')
    unsigned=request_for('content.post_create',{'parent':'/main','body':'unsigned'},
                         app.settings.service_url,subject=user,request_id='unsigned',
                         expires_at=NOW+timedelta(seconds=120))
    expired=replace(packet,expires_at=NOW-timedelta(seconds=1))
    wrong_digest=replace(packet,payload_digest='sha256:'+'0'*64)
    wrong_subject=replace(packet,subject='u_wrong_subject')
    wrong_operation=replace(packet,operation='content.topic_create')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for bad,expected in ((unsigned,'authentication_required'),
                             (expired,'request_expired'),
                             (wrong_digest,'payload_digest_mismatch'),
                             (wrong_subject,'payload_digest_mismatch'),
                             (wrong_operation,'operation_mismatch')):
            path='/-/g/content.post_create/j/'+b64(canonical(bad))
            denied=await http.get(path,headers={'User-Agent':'UnknownClient/1.0'})
            assert denied.json()['error']['code']==expected
        agent_unsigned=await http.get('/-/g/content.post_create/j/'+b64(canonical(unsigned)),
                                      headers={'User-Agent':'AgentRuntime/1.0'})
        assert agent_unsigned.json()['error']['code']=='authentication_required'
        first=await http.get(url,headers={'User-Agent':'AgentRuntime/1.0'})
        assert first.status_code==200,first.text
        replay=await http.get(url,headers={'User-Agent':'UnknownClient/1.0'})
        assert replay.status_code==200 and replay.json()['replayed'] is True
        assert (await post_count(app))[0]==1
        passive_replay=await http.get(url,headers={'User-Agent':'Googlebot'})
        assert passive_replay.status_code==403
        different=replace(packet,request_id='changed-id')
        mismatch=await http.get('/-/g/content.post_create/j/'+b64(canonical(different)),
                                headers={'User-Agent':'AgentRuntime/1.0'})
        assert mismatch.status_code!=200
        assert (await post_count(app))[0]==1
        post_packet=request_for('content.post_create',{'parent':'/main','body':'explicit POST'},
                                app.settings.service_url,signer=key,subject=user,
                                request_id='browser-post',expires_at=NOW+timedelta(seconds=120))
        post=await http.post('/-/p/content.post_create',content=canonical(post_packet),
                             headers={'User-Agent':'Mozilla/5.0 Chrome/140.0'})
        assert post.status_code==200,post.text
        assert (await post_count(app))[0]==2


@pytest.mark.asyncio
async def test_short_code_path_and_public_pages_do_not_spread_execution_credentials(installed):
    app, _ = installed
    key,user,_=await register(app,'short-guard')
    code=build_dictionary(app.registry).code_for('operation','content.post_create@1')
    fake=f'/-/g/{code}/token/cred/SECRET_TOKEN/{user}/once/2026-09-27T00:02:00Z/args'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        blocked=await http.get(fake,headers={'User-Agent':'Discordbot/2.0'})
        assert blocked.status_code==403
        assert blocked.json()['error']['code']=='passive_client_forbidden'
        assert 'SECRET_TOKEN' not in blocked.text
        assert 'noindex' in blocked.headers['x-robots-tag'].lower()
        for path in ('/','/AGENTS.md','/-/d'):
            page=await http.get(path)
            assert page.status_code==200
            assert 'SECRET_TOKEN' not in page.text
            assert '/token/cred/SECRET_TOKEN/' not in page.text
        detail=await http.get('/-/d/content.post_create')
        assert detail.status_code==200
        for operation in detail.json()['operations']:
            template=operation['direct_write_template']
            if template is not None:
                assert '{credential_id}' in template and '{token}' in template
                assert 'SECRET_TOKEN' not in template


@pytest.mark.asyncio
async def test_agent_user_agent_does_not_bypass_current_authorizer(installed):
    app, _ = installed
    key,user,_=await register(app,'agent-not-authority')
    packet=request_for('content.post_create',{'parent':'/private','body':'forbidden'},
                       app.settings.service_url,signer=key,subject=user,
                       request_id='not-authorized',expires_at=NOW+timedelta(seconds=120))
    before=await post_count(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        result=await http.get('/-/g/content.post_create/j/'+b64(canonical(packet)),
                              headers={'User-Agent':'AgentRuntime/1.0'})
        assert result.status_code==403
        assert result.json()['error']['code']=='permission_denied'
    assert await post_count(app)==before
