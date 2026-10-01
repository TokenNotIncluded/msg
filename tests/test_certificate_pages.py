"""Certificate presentation preserves read permission, dates and JSON APIs."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from read_only_evidence import business_snapshot
from test_service import NOW, register

from msg.core.codec import wire
from msg.transports.certificate_pages import certificate_document, status
from msg.transports.home_page import document_html
from msg.transports.http import create_app


@pytest.mark.parametrize(
    'offset,revoked,expected',
    [
        (0, False, 'active'),
        (-86400, False, 'pending'),
        (86400, False, 'expired'),
        (0, True, 'revoked'),
    ],
)
def test_certificate_status_boundaries(offset, revoked, expected):
    value = {
        'certificate': {'not_before': wire(NOW), 'expires_at': wire(NOW + timedelta(days=1))},
        'revoked': revoked,
    }
    assert status(value, NOW + timedelta(seconds=offset)) == expected


def test_document_copy_source_is_literal_and_sharing_omits_query_credentials():
    source = '# 内容\n\n</textarea><script>evil()</script> & `code`'
    html = document_html(
        source, raw_path='/main/article.md', raw_query='limit=2', service_url='https://msg.lmm.best'
    ).decode()
    assert 'id="msg-copy-document"' in html and 'id="msg-share-document"' in html
    assert 'data-share-path="https://msg.lmm.best/main/article.md"' in html
    assert '&lt;/textarea&gt;&lt;script&gt;evil()&lt;/script&gt;' in html
    assert '<script>evil()' not in html
    assert '/main/article.md?limit=2&amp;format=raw' in html


@pytest.mark.asyncio
async def test_certificate_collection_and_detail_render_readonly_with_raw_and_json(installed):
    app, _ = installed
    _, uid, cert_id = await register(app, 'paper-holder')
    before = await business_snapshot(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        collection = await http.get('/@paper-holder/cert', headers={'Accept': 'text/html'})
        assert collection.status_code == 200, collection.text
        assert 'certificate-paper certificate-compact' in collection.text
        assert '@paper-holder' in collection.text and cert_id in collection.text
        assert '/@paper-holder/cert/' + cert_id in collection.text
        path = '/@paper-holder/cert/' + cert_id
        detail = await http.get(path, headers={'Accept': 'text/html'})
        assert detail.status_code == 200, detail.text
        assert 'certificate-seal' in detail.text and 'Authorization scope' in detail.text
        assert 'data-status="active"' in detail.text
        assert 'Signature and technical details' in detail.text
        assert (
            'msg-copy-document' in detail.text
            and 'data-share-path="' + app.settings.service_url + path + '"' in detail.text
        )
        assert 'document.modelContext' in detail.text
        raw = await http.get(path + '?format=raw', headers={'Accept': 'text/html'})
        assert raw.status_code == 200 and raw.headers['content-type'].startswith('text/plain')
        assert raw.text.startswith('# Authorization certificate') and '<!doctype' not in raw.text
        api = await http.get(path + '/json', headers={'Accept': 'text/html'})
        assert api.status_code == 200 and api.headers['content-type'].startswith('application/json')
        assert api.json()['certificate']['subject_id'] == uid
        stable = await http.get('/_id/' + cert_id, headers={'Accept': 'text/html'})
        assert stable.status_code == 200 and 'certificate-seal' in stable.text
        assert (await http.get('/@paper-holder/cert/json')).json()['certificates'][0][
            'id'
        ] == cert_id
        head = await http.head(path, headers={'Accept': 'text/html'})
        assert head.status_code == 200 and not head.content
        assert (await http.post(path)).status_code == 405
        app.clock = lambda: NOW + timedelta(days=365)
        expired = await http.get(
            path, headers={'Accept': 'text/html', 'If-None-Match': detail.headers['etag']}
        )
        assert expired.status_code == 200 and 'data-status="expired"' in expired.text
        assert expired.headers['etag'] != detail.headers['etag']
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_private_certificate_preview_is_not_read_through_its_collection(installed):
    app, _ = installed
    _, uid, cert_id = await register(app, 'private-paper-holder')
    async with app.metadata.transaction(write=False) as tx:
        certificate = wire(await tx.certificate(cert_id))
    # Escaping applies even to a malicious authored display name or grant label.
    evil = {
        'certificate': {**certificate, 'serial': '</dd><script>evil()</script>'},
        'revoked': False,
        'people': {uid: {'name': '<img src=x onerror=evil()>', 'path': '/@private-paper-holder'}},
    }
    html = certificate_document(evil, now=app.clock()).decode()
    assert '<img src=x' not in html and '<script>evil()' not in html
    assert '&lt;img src=x onerror=evil()&gt;' in html
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(cert_id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        collection = await http.get('/@private-paper-holder/cert', headers={'Accept': 'text/html'})
        assert collection.status_code == 200
        assert 'certificate-paper certificate-compact' not in collection.text
        assert certificate['serial'] not in collection.text
        detail = await http.get(
            '/@private-paper-holder/cert/' + cert_id, headers={'Accept': 'text/html'}
        )
        assert detail.status_code == 403
