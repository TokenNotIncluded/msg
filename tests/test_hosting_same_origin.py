"""Hosted executable bytes stay sandboxed on the service origin."""
import httpx
import pytest

from msg.core.codec import b64, loads, wire
from msg.core.models import ResourceRef
from msg.transports.http import create_app
from test_service import call, register


def isolated(response):
    csp=response.headers['content-security-policy'].lower()
    assert 'sandbox' in csp
    assert 'allow-same-origin' not in csp
    assert 'report-only' not in response.headers
    assert 'set-cookie' not in response.headers
    assert response.headers['x-content-type-options']=='nosniff'


@pytest.mark.asyncio
async def test_same_domain_hosted_html_head_304_range_raw_and_no_write_route(installed):
    app, _ = installed
    key,user,_=await register(app,'web-owner')
    site=await call(app,'hosting.create',{'parent':'/@web-owner','name':'web'},
                    key=key,subject=user)
    assert site.status=='ok',wire(site)
    assert site.data['url']=='http://testserver/@web-owner/web/'
    html=b'<!doctype html><script>fetch("/_read/private/json")</script><p>hosted</p>'
    source=await call(app,'content.file_put',{'parent':'/@web-owner/files',
        'name':'page.html','data':b64(html),'media_type':'text/html'},key=key,subject=user)
    svg=await call(app,'content.file_put',{'parent':'/@web-owner/files',
        'name':'drawing.svg','data':b64(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        'media_type':'image/svg+xml'},key=key,subject=user)
    deploy=await call(app,'hosting.deploy',{'id':site.resources[0].id,
        'entries':[{'path':'index.html','source':wire(source.resources[0])},
                   {'path':'drawing.svg','source':wire(svg.resources[0])}]},
        key=key,subject=user,expected=((site.resources[0].id,site.data['generation']),))
    assert deploy.status=='ok',wire(deploy)
    async with app.metadata.transaction(write=False) as tx:
        revision=await tx.revision(ResourceRef(id=site.resources[0].id))
        manifest=loads(await app.contents.read_bytes(revision.content))
        published=manifest['entries']['index.html']['id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        page=await http.get('/@web-owner/web/index.html')
        assert page.status_code==200 and page.content==html
        isolated(page)
        head=await http.head('/@web-owner/web/index.html')
        assert head.status_code==200 and head.content==b''
        isolated(head)
        cached=await http.get('/@web-owner/web/index.html',
                              headers={'If-None-Match':page.headers['etag']})
        assert cached.status_code==304
        isolated(cached)
        partial=await http.get('/@web-owner/web/index.html',headers={'Range':'bytes=0-14'})
        assert partial.status_code==206 and partial.content==html[:15]
        isolated(partial)
        raw=await http.get(f'/_read/{published}/raw')
        assert raw.status_code==200
        assert 'attachment' in raw.headers['content-disposition']
        isolated(raw)
        vector=await http.get('/@web-owner/web/drawing.svg')
        assert vector.status_code==200
        assert 'attachment' in vector.headers['content-disposition']
        isolated(vector)
        missing=await http.get('/@web-owner/web/missing.html')
        assert missing.status_code==404
        isolated(missing)
        for encoded in ('%69ndex.html','%2e%2e/index.html','%2Findex.html'):
            ambiguous=await http.get('/@web-owner/web/'+encoded)
            assert ambiguous.status_code==404
            isolated(ambiguous)
        platform=await http.get('/_read/t_private/json',headers={'Origin':'null',
            'Cookie':'session=should-not-appear','Authorization':'Bearer should-not-appear'})
        assert platform.status_code==403
        assert platform.json()['error']['code']=='forbidden_origin'
        forbidden=await http.post('/@web-owner/web/index.html',content=b'overwrite')
        assert forbidden.status_code==405


@pytest.mark.asyncio
async def test_preview_history_and_atomic_activation_share_same_csp(installed):
    app, _ = installed
    key,user,_=await register(app,'web-versions')
    site=await call(app,'hosting.create',{'parent':'/@web-versions','name':'web'},
                    key=key,subject=user)
    first=await call(app,'content.file_put',{'parent':'/@web-versions/files',
        'name':'v1.html','data':b64(b'<h1>one</h1>'),'media_type':'text/html'},
        key=key,subject=user)
    deployed1=await call(app,'hosting.deploy',{'id':site.resources[0].id,
        'entries':[{'path':'index.html','source':wire(first.resources[0])}]},
        key=key,subject=user,expected=((site.resources[0].id,site.data['generation']),))
    assert deployed1.status=='ok',wire(deployed1)
    old_revision=deployed1.resources[0].revision
    second=await call(app,'content.file_put',{'parent':'/@web-versions/files',
        'name':'v2.html','data':b64(b'<h1>two</h1>'),'media_type':'text/html'},
        key=key,subject=user)
    deployed2=await call(app,'hosting.deploy',{'id':site.resources[0].id,
        'entries':[{'path':'index.html','source':wire(second.resources[0])}]},
        key=key,subject=user,expected=((site.resources[0].id,deployed1.data['generation']),))
    assert deployed2.status=='ok',wire(deployed2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        current=await http.get('/@web-versions/web/index.html')
        historical=await http.get(f'/@web-versions/web/_rev/{old_revision}/index.html')
        assert current.content==b'<h1>two</h1>' and historical.content==b'<h1>one</h1>'
        isolated(current);isolated(historical)
    rollback=await call(app,'hosting.activate',{'id':site.resources[0].id,
        'revision':old_revision},key=key,subject=user,
        expected=((site.resources[0].id,deployed2.data['generation']),))
    assert rollback.status=='ok',wire(rollback)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        rolled_back=await http.get('/@web-versions/web/index.html')
        assert rolled_back.content==b'<h1>one</h1>'
        isolated(rolled_back)


@pytest.mark.asyncio
async def test_root_sample_is_public_sandboxed_and_never_uses_root_credentials(installed):
    app, _ = installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        sample=await http.get('/@root/web/index.html',headers={'Origin':'null',
            'Authorization':'Bearer should-not-appear','Cookie':'session=should-not-appear'})
        assert sample.status_code==200
        isolated(sample)
        assert b'should-not-appear' not in sample.content
