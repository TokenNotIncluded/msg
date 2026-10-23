"""Verify homepage metadata without changing Markdown or non-home resources."""

import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTML = b'<!doctype html><html><head><title>msg</title></head><body>Public home</body></html>'
MARKDOWN = b'# msg\n\nAgent-readable public home.\n'


class Backend(BaseHTTPRequestHandler):
    def do_GET(self):
        browser = 'text/html' in self.headers.get('Accept', '')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if browser else 'text/markdown')
        self.send_header('Content-Security-Policy', "default-src 'none'")
        self.end_headers()
        self.wfile.write(HTML if browser else MARKDOWN)

    def log_message(self, *args):
        pass


backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
thread = threading.Thread(target=backend.serve_forever, daemon=True)
thread.start()
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]


def get(path, accept):
    with urllib.request.urlopen(
        urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={'Accept': accept}),
        timeout=3,
    ) as response:
        return response.headers, response.read()


try:
    with tempfile.TemporaryDirectory(prefix='msg-home-seo-') as temp:
        directory = Path(temp)
        conf = directory / 'nginx.conf'
        snippet = Path(__file__).with_name('nginx-home-seo.conf').read_text()
        conf.write_text(
            f'pid {directory}/nginx.pid; error_log {directory}/error.log; daemon off; master_process off; events {{}}\n'
            'http { access_log off;\n'
            + ''.join(
                f'{kind}_temp_path {directory}/{kind};\n'
                for kind in ['client_body', 'proxy', 'fastcgi', 'uwsgi', 'scgi']
            )
            + f'upstream msg_lmm_best {{ server 127.0.0.1:{backend.server_port}; }}\n'
            'map $uri $msg_browser_cache_control { default "private, no-cache"; }\n'
            'map $uri $msg_browser_referrer_policy { default "no-referrer"; }\n'
            + f'server {{ listen 127.0.0.1:{port};\n'
            + snippet
            + 'location / { proxy_pass http://msg_lmm_best; }\n}}\n'
        )
        process = subprocess.Popen(
            ['nginx', '-p', temp, '-c', str(conf)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            for _attempt in range(40):
                try:
                    get('/', 'text/html')
                    break
                except urllib.error.URLError:
                    if process.poll() is not None:
                        raise RuntimeError(process.communicate()[1].decode()) from None
                    time.sleep(0.05)
            else:
                raise RuntimeError('Nginx did not start')
            headers, body = get('/', 'text/html')
            assert body.count(b'rel="canonical"') == 1 and b'name="description"' in body
            assert headers['Content-Security-Policy'] == "default-src 'none'"
            assert headers['Cache-Control'] == 'private, no-cache'
            assert headers['Link'] == '<https://msg.lmm.best/>; rel="canonical"'
            headers, body = get('/', 'text/markdown')
            assert body == MARKDOWN and headers['Content-Type'].startswith('text/markdown')
            assert get('/another-resource', 'text/html')[1] == HTML
            print(
                'HTML metadata, canonical header, CSP/cache, unchanged Markdown and other resources passed'
            )
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
finally:
    backend.shutdown()
    backend.server_close()
