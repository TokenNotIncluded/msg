"""Exercise the cache allowlist through the complete, real TLS ingress."""

import http.client
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_nginx_secret_logs import _application, _proxy


def _request(proxy, target, *, method='GET', headers=None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Disposable local certificate only.
    connection = http.client.HTTPSConnection('127.0.0.1', proxy.tls, timeout=3, context=context)
    try:
        connection.putrequest(method, target, skip_host=True)
        pairs = headers.items() if isinstance(headers, dict) else (headers or [])
        for name, value in [('Host', 'msg.example.org'), *pairs]:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


def _header_values(response, name):
    return [value for key, value in response[1] if key.casefold() == name.casefold()]


def _assert_policy(response, expected, *, status=200):
    assert response[0] == status
    # Keep the raw list: converting it to a dict would conceal duplicate fields.
    assert _header_values(response, 'Cache-Control') == [expected]
    assert _header_values(response, 'X-Content-Type-Options') == ['nosniff']


@contextmanager
def _policy_backend():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self):
            status = int(self.headers.get('X-Fixture-Status', '200'))
            policy = self.headers.get('X-Fixture-Policy', 'normal')
            cache = (
                'public, max-age=300'
                if self.path.startswith(('/install', '/favicon.png'))
                else 'private, no-cache'
            )
            self.send_response(status)
            if policy == 'normal':
                self.send_header('Cache-Control', cache)
            elif policy == 'duplicate':
                self.send_header('Cache-Control', cache)
                self.send_header('Cache-Control', cache)
            elif policy == 'conflicting':
                self.send_header('Cache-Control', cache)
                self.send_header('Cache-Control', 'public, max-age=86400')
            elif policy != 'missing':
                self.send_header('Cache-Control', policy)
            cookie = self.headers.get('X-Fixture-Set-Cookie')
            if cookie == 'duplicate':
                self.send_header('Set-Cookie', '')
            if cookie:
                self.send_header('Set-Cookie', 'msg_fixture=nonlive; Secure; HttpOnly')
            self.send_header('ETag', '"nonlive-fixture"')
            self.send_header('Content-Length', '0')
            self.end_headers()

        do_GET = do_HEAD = do_POST = respond

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_real_application_cache_headers_and_conditional_reads(tmp_path):
    with _application() as backend, _proxy(tmp_path, backend) as proxy:
        for target, expected in (
            ('/', 'private, no-cache'),
            ('/install', 'public, max-age=300'),
            ('/favicon.png', 'public, max-age=300'),
        ):
            headers = {'Accept': 'text/html'} if target == '/' else {}
            page = _request(proxy, target, headers=headers)
            _assert_policy(page, expected)
            assert page[2]
            (etag,) = _header_values(page, 'ETag')
            head = _request(proxy, target, method='HEAD', headers=headers)
            _assert_policy(head, expected)
            assert head[2] == b''
            for method in ('GET', 'HEAD'):
                conditional = _request(
                    proxy, target, method=method, headers={**headers, 'If-None-Match': etag}
                )
                _assert_policy(conditional, expected, status=304)
                assert conditional[2] == b''
            # Even fixed assets carrying an unrelated browser cookie stay no-store.
            _assert_policy(
                _request(proxy, target, headers={**headers, 'Cookie': 'nonlive=1'}), 'no-store'
            )


def test_actual_application_explicit_credentials_remain_no_store(tmp_path):
    with _application() as backend, _proxy(tmp_path, backend) as proxy:
        for name in ('Authorization', 'X-Msg-Request'):
            headers = {name: 'nonlive'}
            for target in ('/', '/install', '/favicon.png'):
                _assert_policy(_request(proxy, target, headers=headers), 'no-store')
            for target in ('/now', '/_post/state'):
                rejected = _request(proxy, target, headers=headers)
                _assert_policy(rejected, 'no-store', status=400)
                assert b'nonlive' not in rejected[2]


def test_allowlist_rejects_queries_aliases_other_paths_and_credentials(tmp_path):
    with _policy_backend() as backend, _proxy(tmp_path, backend) as proxy:
        for target in (
            '/?q=ordinary',
            '/?',
            '/install?',
            '/install?token=nonlive',
            '/favicon.png?q=ordinary',
            '/favicon.png?',
            '/%69nstall',
            '/./install',
            '//install',
            '/x/../install',
            '/INSTALL',
            '/install/',
            '/faviconXpng',
            '/healthz',
            '/AGENTS.md',
            '/_rules',
            '/-/p/discovery.get',
            '/@nonlive/files/private',
            '/_flight',
        ):
            _assert_policy(_request(proxy, target), 'no-store')
        for target in ('/', '/install', '/favicon.png'):
            for name in (
                'Cookie',
                'Authorization',
                'X-Msg-Request',
                'X-Msg-Signature',
                'X-Msg-Proof',
            ):
                for value in ('nonlive', ':'):
                    _assert_policy(_request(proxy, target, headers={name: value}), 'no-store')
            _assert_policy(_request(proxy, target, method='POST'), 'no-store')
            _assert_policy(
                _request(proxy, target, headers={'X-Fixture-Set-Cookie': 'yes'}), 'no-store'
            )


def test_duplicate_request_credentials_and_upstream_cookies_remain_no_store(tmp_path):
    with _policy_backend() as backend, _proxy(tmp_path, backend) as proxy:
        for target in ('/', '/install', '/favicon.png'):
            for name in (
                'Cookie',
                'Authorization',
                'X-Msg-Request',
                'X-Msg-Signature',
                'X-Msg-Proof',
            ):
                for values in (('', 'nonlive'), ('nonlive', '')):
                    response = _request(proxy, target, headers=[(name, value) for value in values])
                    # Nginx rejects duplicate Authorization before proxying it.
                    _assert_policy(response, 'no-store', status=response[0])
                    assert response[0] in (200, 400)
            _assert_policy(
                _request(proxy, target, headers={'X-Fixture-Set-Cookie': 'duplicate'}), 'no-store'
            )


@pytest.mark.parametrize('target', ['/', '/install', '/favicon.png'])
def test_allowlist_rejects_untrusted_upstream_cache_values_and_statuses(tmp_path, target):
    with _policy_backend() as backend, _proxy(tmp_path, backend) as proxy:
        expected = 'private, no-cache' if target == '/' else 'public, max-age=300'
        _assert_policy(_request(proxy, target), expected)
        for policy in (
            'missing',
            'duplicate',
            'conflicting',
            'no-store',
            'private, no-store',
            'public, max-age=86400',
            'private, no-cache, public',
            'PUBLIC, MAX-AGE=300',
            'public,max-age=300',
            'public, max-age=300, immutable',
            'public, max-age=300' if target == '/' else 'private, no-cache',
        ):
            _assert_policy(
                _request(proxy, target, headers={'X-Fixture-Policy': policy}), 'no-store'
            )
        for status in (201, 204, 206, 301, 400, 401, 404, 500):
            _assert_policy(
                _request(proxy, target, headers={'X-Fixture-Status': str(status)}),
                'no-store',
                status=status,
            )
        _assert_policy(
            _request(proxy, target, headers={'X-Fixture-Status': '304'}), expected, status=304
        )


def test_proxy_generated_failure_on_allowlisted_uri_remains_no_store(tmp_path):
    # A refused upstream must not inherit the successful response exception.
    with _policy_backend() as closed_port:
        pass
    with _proxy(tmp_path, closed_port) as proxy:
        for target in ('/', '/install', '/favicon.png'):
            _assert_policy(_request(proxy, target), 'no-store', status=502)
