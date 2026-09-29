"""Conditional byte reads must cross the same live authority as projections."""

from datetime import timedelta

from test_service import NOW

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http_common import error_status


async def assert_read_cache_matrix(http, app, reader, certs, rid, revision, expected, cache):
    denial_code = 'certificate_revoked' if certs else 'permission_denied'
    denial_status = error_status(denial_code)
    representations = (
        ('json', None),
        ('meta', None),
        ('history', None),
        ('raw', None),
        ('raw', revision),
    )
    for view, version in representations:
        path = '/_id/' + rid + ('/revisions/' + version if version else '') + '/' + view
        args = {'id': rid}
        if version:
            args['revision'] = version
        if view in {'meta', 'history'}:
            args['view'] = view
        packet = request_for(
            'discovery.raw' if view == 'raw' else 'discovery.get',
            args,
            app.settings.service_url,
            signer=reader[0],
            subject=reader[1],
            certificates=certs,
            expires_at=NOW + timedelta(seconds=60),
        )
        auth = {'x-msg-request': b64(canonical(packet))}
        first = await http.get(path, headers=auth)
        if expected == 'ok':
            assert first.status_code == 200, (view, version, first.text)
            cache[path] = first.headers['etag']
            if view == 'raw':
                assert first.content == b'source-matrix-private'
        else:
            assert first.status_code == denial_status, (view, version, first.text)
            assert first.json()['error']['code'] == denial_code
        tag = cache[path]
        cases = [({}, 200), ({'if-none-match': tag}, 304)]
        if view == 'raw':
            cases += [
                ({'range': 'bytes=0-5'}, 206),
                ({'range': 'bytes=0-5', 'if-range': tag}, 206),
                ({'range': 'bytes=0-5', 'if-range': '"outdated"'}, 200),
                ({'range': 'bytes=999999-'}, 416),
            ]
        for method in ('GET', 'HEAD'):
            for extra, status in cases:
                response = await http.request(method, path, headers={**auth, **extra})
                assert response.status_code == (status if expected == 'ok' else denial_status), (
                    method,
                    view,
                    version,
                    extra,
                    response.status_code,
                    response.text,
                )
                if method == 'HEAD':
                    assert response.content == b''
                if expected != 'ok':
                    assert not any(
                        name in response.headers
                        for name in ('etag', 'location', 'content-range', 'content-disposition')
                    )
                    assert 'source-matrix-private' not in response.text
                elif status == 206:
                    assert response.headers['content-range'] == 'bytes 0-5/21'
                    assert response.headers['content-length'] == '6'
                    if method == 'GET':
                        assert response.content == b'source'
