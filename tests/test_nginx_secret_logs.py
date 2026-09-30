"""Run the complete deployment config, including TLS/default/error contexts.

The application fixture only stubs read-only homepage discovery: actual HTTP
boundary rejections and health/root reads are exercised here. PostgreSQL/authentication
and zero-write assertions remain in test_url_secrets.py.
"""

import gzip
import http.client
import logging
import re
import shutil
import socket
import ssl
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import uvicorn

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app

NGINX_CONFIG = Path(__file__).parents[1] / 'deploy/nginx.conf'


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_nginx_policy_covers_pre_host_parsing_and_both_default_listeners():
    source = NGINX_CONFIG.read_text()
    assert re.search(r'(?m)^error_log\s+/dev/null\s+crit;', source)
    assert re.search(r'(?m)^events\s*\{', source)
    assert re.search(r'(?m)^http\s*\{', source)
    assert source.count('default_server;') == 4  # IPv4 and IPv6, HTTP and HTTPS.
    assert re.search(r'(?m)^\s+access_log\s+off;', source)
    assert not re.search(r'(?m)^\s*include\s', source)
    assert 'return 421;' in source and 'return 400;' in source
    assert not re.search(r'\breturn\s+30[1278]\b', source)
    assert 'proxy_pass http://127.0.0.1:8042;' in source
    assert 'proxy_set_header Referer "";' in source
    assert '$request_uri' not in source


@contextmanager
def _proxy(tmp_path, backend_port, *, unsafe_control=False):
    # Dependencies are required, not skipped. CI installs them explicitly.
    assert shutil.which('nginx'), 'install nginx to run ingress security tests'
    assert shutil.which('openssl'), 'install openssl to run ingress TLS tests'
    source = NGINX_CONFIG.read_text()
    assert re.search(r'(?m)^http\s*\{', source), 'a complete dedicated config is required'
    http_port, tls_port = _free_port(), _free_port()
    key, cert = tmp_path / 'key.pem', tmp_path / 'cert.pem'
    subprocess.run(
        [
            'openssl',
            'req',
            '-x509',
            '-newkey',
            'rsa:2048',
            '-nodes',
            '-keyout',
            str(key),
            '-out',
            str(cert),
            '-days',
            '1',
            '-subj',
            '/CN=msg.example.org',
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    source = source.replace('worker_processes auto;', 'worker_processes 1;')
    source = re.sub(r'(?m)^pid [^;]+;', f'pid {tmp_path}/nginx.pid;', source)
    source = source.replace(
        'listen 80 default_server;', f'listen 127.0.0.1:{http_port} default_server;'
    )
    source = source.replace(
        'listen 443 ssl default_server;', f'listen 127.0.0.1:{tls_port} ssl default_server;'
    )
    source = re.sub(r'(?m)^\s*listen \[::\]:[^;]+;\s*$', '', source)
    source = re.sub(r'ssl_certificate [^;]+;', f'ssl_certificate {cert};', source)
    source = re.sub(r'ssl_certificate_key [^;]+;', f'ssl_certificate_key {key};', source)
    source = source.replace('http://127.0.0.1:8042', f'http://127.0.0.1:{backend_port}')
    temp_directives = []
    for name in ('client_body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'):
        directory = tmp_path / name
        directory.mkdir()
        temp_directives.append(f'    {name}_temp_path {directory};')
    source = re.sub(r'(?m)^http \{', lambda _: 'http {\n' + '\n'.join(temp_directives), source)
    if unsafe_control:
        source = source.replace('access_log off;', f'access_log {tmp_path}/unsafe.log;')
    config = tmp_path / 'nginx.conf'
    config.write_text(source)
    check = subprocess.run(
        ['nginx', '-t', '-c', str(config), '-p', str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert check.returncode == 0, check.stderr
    error_file = tmp_path / 'process.log'
    with error_file.open('wb') as errors:
        process = subprocess.Popen(
            [
                'nginx',
                '-g',
                'daemon off; master_process off;',
                '-c',
                str(config),
                '-p',
                str(tmp_path),
            ],
            stdout=errors,
            stderr=errors,
        )
        try:
            for _ in range(200):
                try:
                    with socket.create_connection(('127.0.0.1', tls_port), timeout=0.2):
                        break
                except OSError:
                    assert process.poll() is None, 'nginx exited during startup'
                    threading.Event().wait(0.02)
            else:
                pytest.fail('nginx did not begin listening')
            yield SimpleNamespace(http=http_port, tls=tls_port, process=process)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@contextmanager
def _application():
    class HomepageExecutor:
        async def require_current_runtime(self):
            pass

        def recovery_drill_active(self):
            return False

        async def execute(self, packet, *, entry):
            # The homepage is the only read exercised by this boundary fixture.
            # Any attempted business execution still fails immediately.
            assert packet.operation == 'discovery.read_query'
            assert entry == 'network'
            assert packet.arguments == {'home_summary': True}
            return SimpleNamespace(
                error=None,
                data={
                    'posts': 0,
                    'posts_today': 0,
                    'users': 0,
                    'date': '2026-09-30',
                    'timezone': 'Asia/Taipei',
                    'latest': [],
                },
            )

    class BoundaryRegistry:
        def operation(self, name, *_args):
            # These specs only enable real packet parsing/rejection. Any request
            # reaching business execution fails in the homepage-only executor.
            return SimpleNamespace(name=name, entries=('network',), effect='transaction')

    service = SimpleNamespace(
        _loaded=True,
        executor=HomepageExecutor(),
        registry=BoundaryRegistry(),
        settings=SimpleNamespace(
            service_url='https://msg.example.org',
            server=SimpleNamespace(
                limits=SimpleNamespace(
                    max_path_bytes=8192, max_request_bytes=1048576, max_response_bytes=1048576
                )
            ),
        ),
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service),
            host='127.0.0.1',
            port=port,
            access_log=False,
            log_config=None,
            lifespan='off',
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            assert thread.is_alive(), 'uvicorn exited during startup'
            threading.Event().wait(0.01)
        else:
            pytest.fail('uvicorn did not begin listening')
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


def _request(
    proxy, target, *, method='GET', host='msg.example.org', plain=False, headers=None, body=None
):
    # Do not use a client logger which would itself record the test secret URL.
    if plain:
        connection = http.client.HTTPConnection('127.0.0.1', proxy.http, timeout=3)
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Disposable self-signed test certificate only.
        connection = http.client.HTTPSConnection('127.0.0.1', proxy.tls, timeout=3, context=context)
    try:
        connection.request(
            method,
            target,
            body=body,
            headers={'Host': host, 'User-Agent': 'AgentRuntime/1', **(headers or {})},
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _assert_safe(response, status, markers):
    code, headers, body = response
    assert code == status
    assert headers.get('Referrer-Policy', headers.get('referrer-policy')) == 'no-referrer'
    assert headers.get('Cache-Control', headers.get('cache-control')) == 'no-store'
    combined = str(headers).encode() + body
    assert all(marker.encode() not in combined for marker in markers), 'response leaked a sentinel'


def _assert_logs_safe(tmp_path, caplog, markers):
    logs = caplog.text.encode() + b''.join(path.read_bytes() for path in tmp_path.rglob('*.log'))
    assert all(marker.encode() not in logs for marker in markers), 'a log leaked a sentinel'


def test_actual_tls_proxy_application_and_early_error_matrix(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger='msg')
    caplog.set_level(logging.DEBUG, logger='uvicorn')
    secret = 'nonlive_' + uuid4().hex
    token = secret.encode()
    packet = request_for(
        'content.post_create',
        {'parent': '/main', 'body': 'must not execute'},
        'https://msg.example.org',
        subject='u_test',
        token=('c_test', token),
    )
    raw = canonical(packet)
    encoded = [b64(raw), b64(gzip.compress(raw, mtime=0))]
    markers = [secret, b64(token), *encoded]
    with _application() as port, _proxy(tmp_path, port) as proxy:
        _assert_safe(_request(proxy, '/healthz'), 200, markers)
        _assert_safe(_request(proxy, '/?q=token'), 200, markers)
        # Authorization remains a header-only channel; Referer is stripped.
        headers = {
            'Authorization': 'Bearer ' + secret,
            'Referer': 'https://example.org/?token=' + secret,
        }
        _assert_safe(_request(proxy, '/healthz', headers=headers), 200, markers)
        for target in (
            '/?token=' + secret,
            '/?%2572ecovery_secret=' + secret,
            '/?ordinary=x%26token=' + secret,
            '/-/g/content.post_create/%2574oken/' + secret,
        ):
            for method in ('GET', 'HEAD', 'POST'):
                _assert_safe(_request(proxy, target, method=method, headers=headers), 400, markers)
        for encoding, value in zip(('j', 'gz'), encoded, strict=True):
            target = '/-/g/content.post_create/' + encoding + '/' + value
            for method in ('GET', 'HEAD'):
                _assert_safe(_request(proxy, target, method=method, headers=headers), 400, markers)
        for plain in (False, True):
            # An unknown Host never falls into an unrelated default server.
            _assert_safe(
                _request(
                    proxy, '/?token=' + secret, host='wrong.example', plain=plain, headers=headers
                ),
                400 if plain else 421,
                markers,
            )
            # Nginx generates these errors before the application sees a request.
            _assert_safe(
                _request(
                    proxy, '/?' + secret, plain=plain, headers={'X-Large': secret + 'x' * 20000}
                ),
                400,
                markers,
            )
            _assert_safe(
                _request(proxy, '/' + secret + 'x' * 20000, plain=plain, headers=headers),
                414,
                markers,
            )
        _assert_safe(
            _request(
                proxy, '/healthz', method='POST', headers={**headers, 'Content-Length': '1048577'}
            ),
            413,
            markers,
        )
        # Between application and Nginx limits: app-level bounded target failure.
        _assert_safe(_request(proxy, '/healthz?q=' + secret + 'x' * 9000), 413, markers)
        # Malformed host/URI cannot enter a log even when Nginx owns the error.
        _assert_safe(_request(proxy, '/%GG' + secret), 400, markers)
        # Cleartext is deliberately rejected, not redirected with the input URL.
        response = _request(proxy, '/healthz?token=' + secret, plain=True)
        _assert_safe(response, 400, markers)
        assert 'Location' not in response[1]
    _assert_logs_safe(tmp_path, caplog, markers)


@pytest.mark.parametrize('disconnect', [False, True])
def test_upstream_refusal_and_disconnect_do_not_log_request(tmp_path, caplog, disconnect):
    secret = 'nonlive_' + uuid4().hex
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    thread = None
    if disconnect:
        listener.listen()
        listener.settimeout(5)

        def close_upstream():
            connection, _ = listener.accept()
            with connection:
                connection.recv(65536)
                # Simulate a backend disappearing without sending response headers.

        thread = threading.Thread(target=close_upstream, daemon=True)
        thread.start()
    else:
        listener.close()
    try:
        with _proxy(tmp_path, port) as proxy:
            response = _request(
                proxy,
                '/?token=' + secret,
                headers={
                    'Referer': 'https://example.org/' + secret,
                    'Authorization': 'Bearer ' + secret,
                },
            )
            _assert_safe(response, 502, [secret])
    finally:
        listener.close()
        if thread is not None:
            thread.join(timeout=6)
            assert not thread.is_alive()
    _assert_logs_safe(tmp_path, caplog, [secret])


def test_log_sentinel_check_detects_an_insecure_control(tmp_path):
    secret = 'nonlive_' + uuid4().hex
    # The negative control changes only the logging policy of the same config.
    with _proxy(tmp_path, _free_port(), unsafe_control=True) as proxy:
        assert _request(proxy, '/?token=' + secret, plain=True)[0] == 400
    assert secret in (tmp_path / 'unsafe.log').read_text()
