"""The checked-in Nginx listener must not log credential-bearing URLs."""
import http.client
import re
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


NGINX_CONFIG = Path(__file__).parents[1] / 'deploy/nginx.conf'


def _server_block():
    source = NGINX_CONFIG.read_text()
    match = re.search(r'(?ms)^server\s*\{.*?^\}', source)
    assert match, 'deploy/nginx.conf must contain a server block'
    return match.group(0)


def test_nginx_server_disables_request_logs_and_keeps_proxy_get():
    block = _server_block()
    assert re.search(r'(?m)^\s*access_log\s+off\s*;', block)
    assert re.search(r'(?m)^\s*error_log\s+/dev/null\s+crit\s*;', block)
    assert re.search(r'(?m)^\s*proxy_pass\s+http://127\.0\.0\.1:8042\s*;', block)
    assert re.search(r'(?m)^\s*location\s+/\s*\{', block)


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(shutil.which('nginx') is None, reason='nginx binary is unavailable')
def test_nginx_config_suppresses_secret_url_logs_and_serves_normal_get(tmp_path):
    backend_requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            backend_requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *_args):
            pass

    backend = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    listen_port = _free_port()
    block = _server_block()
    block = re.sub(r'(?m)^\s*listen\s+443\s+ssl\s*;\s*$',
                   f'    listen 127.0.0.1:{listen_port};', block)
    block = re.sub(r'(?m)^\s*ssl_certificate(?:_key)?\s+[^;]+;\s*\n', '', block)
    block = re.sub(r'(?m)^\s*ssl_protocols\s+[^;]+;\s*\n', '', block)
    block = block.replace('http://127.0.0.1:8042',
                          f'http://127.0.0.1:{backend.server_port}')
    config_path = tmp_path / 'nginx.conf'
    temp_directories = [tmp_path / name for name in
                        ('client-body', 'proxy', 'fastcgi', 'uwsgi', 'scgi')]
    for directory in temp_directories:
        directory.mkdir()
    temp_directives = ''.join(
        f'    {directive}_temp_path {directory};\n'
        for directive, directory in zip(
            ('client_body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'), temp_directories
        )
    )
    config_path.write_text(
        f'pid {tmp_path / "nginx.pid"};\n'
        f'error_log {tmp_path / "nginx-error.log"} notice;\n'
        'events {}\nhttp {\n'
        f'    access_log {tmp_path / "access.log"};\n'
        + temp_directives + block + '\n}\n'
    )

    check = subprocess.run(['nginx', '-t', '-c', str(config_path), '-p', str(tmp_path)],
                           capture_output=True, text=True, timeout=10)
    assert check.returncode == 0, check.stderr
    process = subprocess.Popen(
        ['nginx', '-g', 'daemon off;', '-c', str(config_path), '-p', str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(100):
            try:
                connection = http.client.HTTPConnection('127.0.0.1', listen_port, timeout=1)
                connection.request('GET', '/normal')
                response = connection.getresponse()
                assert response.status == 200
                assert response.read() == b'ok'
                connection.close()
                break
            except OSError:
                if process.poll() is not None:
                    pytest.fail(f'nginx exited during startup: {process.stderr.read()}')
                threading.Event().wait(0.02)
        else:
            pytest.fail('nginx did not begin listening')

        for target in ('/?token=QUERY_SECRET', '/-/g/content.post_create/token/PATH_SECRET'):
            connection = http.client.HTTPConnection('127.0.0.1', listen_port, timeout=2)
            connection.request('GET', target)
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            connection.close()

        assert backend_requests == [
            '/normal', '/?token=QUERY_SECRET', '/-/g/content.post_create/token/PATH_SECRET'
        ]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()
        backend_thread.join(timeout=2)

    error_log = (tmp_path / 'nginx-error.log').read_text()
    assert 'QUERY_SECRET' not in error_log
    assert 'PATH_SECRET' not in error_log
    access_log = tmp_path / 'access.log'
    assert not access_log.exists() or access_log.read_text() == ''
