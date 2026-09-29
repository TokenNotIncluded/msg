from datetime import timedelta

import httpx
import pytest
from test_legacy_resource_import import setup
from test_service import NOW, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.storage.legacy_resource_import import PURPOSE, import_content
from msg.transports.http import create_app


def signed(app, key, subject, rid, view=None):
    args = {'id': rid}
    if view == 'meta':
        args['view'] = 'meta'
    packet = request_for(
        'discovery.raw' if view == 'raw' else 'discovery.get',
        args,
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=NOW + timedelta(minutes=2),
    )
    return {'x-msg-request': b64(canonical(packet))}


@pytest.mark.asyncio
async def test_import_http_requires_current_signature_and_preserves_provenance(installed, tmp_path):
    app, root, source, approval, key, cert = await setup(installed, tmp_path)
    report = await import_content(
        app, source, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
    )
    prefix = '/_legacy/' + approval['source_sha256']
    rid = report['url_map']['/main/17']
    async with app.metadata.transaction(write=False) as tx:
        provenance = tx.setting('legacy-provenance:' + rid)['resource']
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
        follow_redirects=False,
    ) as http:
        for method in ('GET', 'HEAD'):
            for suffix, view in (('', None), ('/raw', 'raw'), ('/meta', 'meta')):
                denied = await http.request(
                    method,
                    prefix + '/main/17' + suffix,
                    headers={'if-none-match': '"warm"', 'range': 'bytes=0-1'},
                )
                assert denied.status_code == 403, denied.text
                assert 'location' not in denied.headers and 'link' not in denied.headers
                assert 'etag' not in denied.headers and 'old body' not in denied.text
                allowed = await http.request(
                    method,
                    prefix + '/main/17' + suffix,
                    headers=signed(app, key, approval['operator'], rid, view),
                )
                assert allowed.status_code == 308, allowed.text
                assert allowed.headers['cache-control'] == 'no-store'
                assert allowed.headers['x-msg-legacy-signature'] == 'unverified-historical-claim'
                assert '/provenance>' in allowed.headers['link']
                followed = await http.request(
                    method,
                    allowed.headers['location'],
                    headers=signed(app, key, approval['operator'], rid, view),
                )
                assert followed.status_code == 200, followed.text
        history = await http.get(
            prefix + '/main/17/provenance',
            headers=signed(app, key, approval['operator'], provenance),
        )
        assert history.status_code == 308, history.text
        value = await http.get(
            history.headers['location'], headers=signed(app, key, approval['operator'], provenance)
        )
        assert value.status_code == 200 and 'historical-signature' in value.text
        file_id = report['url_map']['/file/1']
        attachment = await http.get(
            prefix + '/file/1', headers=signed(app, key, approval['operator'], file_id, 'raw')
        )
        assert attachment.status_code == 308, attachment.text
        raw = await http.get(
            attachment.headers['location'],
            headers=signed(app, key, approval['operator'], file_id, 'raw'),
        )
        assert raw.content == b'abc'
        # No implicit takeover of current /main routes or write alias.
        assert (await http.post(prefix + '/main/17')).status_code == 405
        assert (await http.get('/main/17')).status_code == 404
        assert (await http.get('/_legacy/' + '0' * 64 + '/main/17')).status_code == 404
        outsider, other, _ = await register(app, 'legacy-outsider')
        denied = await http.get(prefix + '/main/17', headers=signed(app, outsider, other, rid))
        assert denied.status_code == 403 and 'location' not in denied.headers
        # Authorization is live even after a formerly successful redirect.
        async with app.metadata.transaction(write=True) as tx:
            from dataclasses import replace

            current = await tx.resource(rid)
            await tx.replace(
                replace(current, mode=0, generation=current.generation + 1), current.generation
            )
        denied = await http.get(
            prefix + '/main/17', headers=signed(app, key, approval['operator'], rid)
        )
        assert denied.status_code == 403 and 'location' not in denied.headers


@pytest.mark.asyncio
async def test_legacy_mapping_race_and_namespace_escape_do_not_redirect(
    installed, tmp_path, monkeypatch
):
    from dataclasses import replace

    app, root, source, approval, key, cert = await setup(installed, tmp_path)
    report = await import_content(
        app, source, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
    )
    sha256 = approval['source_sha256']
    path = '/_legacy/' + sha256 + '/main/17'
    rid = report['url_map']['/main/17']
    execute = app.executor.execute
    changed = False

    async def replace_mapping(packet, **kwargs):
        nonlocal changed
        if not changed and packet.arguments.get('id') == rid:
            changed = True
            async with app.metadata.transaction(write=True) as tx:
                record = tx.setting('legacy-import:' + sha256)
                record['url_map']['/main/17'] = tx.setting('legacy-provenance:' + rid)['resource']
                tx.set_setting('legacy-import:' + sha256, record)
        return await execute(packet, **kwargs)

    monkeypatch.setattr(app.executor, 'execute', replace_mapping)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get(path, headers=signed(app, key, approval['operator'], rid))
        assert response.status_code == 400, response.text
        assert 'location' not in response.headers and 'link' not in response.headers
        async with app.metadata.transaction(write=True) as tx:
            record = tx.setting('legacy-import:' + sha256)
            record['url_map']['/main/17'] = rid
            tx.set_setting('legacy-import:' + sha256, record)
            current = await tx.resource(rid)
            await tx.replace(
                replace(
                    current, parent=await tx.resolve('/main'), generation=current.generation + 1
                ),
                current.generation,
            )
        escaped = await http.get(path, headers=signed(app, key, approval['operator'], rid))
        assert escaped.status_code == 404 and 'location' not in escaped.headers
