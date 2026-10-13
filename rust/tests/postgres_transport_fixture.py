"""One local-test-only PostgreSQL proxy that loses a committed acknowledgement.

The target must be an explicitly supplied loopback or Unix socket test instance.
It never sends authentication data anywhere other than that local test server.
"""

from __future__ import annotations

import socket
import threading
from contextlib import AbstractContextManager

from psycopg.conninfo import conninfo_to_dict, make_conninfo


def exact(sock, n):
    data = bytearray()
    while len(data) < n:
        block = sock.recv(n - len(data))
        if not block:
            raise EOFError
        data.extend(block)
    return bytes(data)


class LoseCommitAck(AbstractContextManager):
    def __init__(self, dsn):
        self.config = conninfo_to_dict(dsn)
        if not self.config.get('dbname', '').startswith('msg_rust_test_'):
            raise ValueError('disposable database required')
        host = self.config.get('host', '')
        if not (host.startswith('/') or host in {'localhost', '127.0.0.1', '::1'}):
            raise ValueError('local test host required')
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.dsn = make_conninfo(
            dsn, host='127.0.0.1', port=self.listener.getsockname()[1], sslmode='disable'
        )
        self.closed = threading.Event()
        self.dropped = threading.Event()
        self.sockets = []
        self.threads = []

    def __enter__(self):
        thread = threading.Thread(target=self.accept, daemon=True)
        self.threads.append(thread)
        thread.start()
        return self

    def accept(self):
        while not self.closed.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.sockets.append(client)
            thread = threading.Thread(target=self.connection, args=(client,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def connection(self, client):
        backend = None
        try:
            host = self.config['host']
            port = int(self.config.get('port', 5432))
            if host.startswith('/'):
                backend = socket.socket(socket.AF_UNIX)
                backend.connect(host + '/.s.PGSQL.' + str(port))
            else:
                backend = socket.create_connection((host, port), timeout=5)
            self.sockets.append(backend)
            size = exact(client, 4)
            n = int.from_bytes(size, 'big')
            backend.sendall(size + exact(client, n - 4))
            wrote = threading.Event()
            dropping = threading.Event()

            def responses():
                try:
                    while not self.closed.is_set():
                        tag = exact(backend, 1)
                        size = exact(backend, 4)
                        n = int.from_bytes(size, 'big')
                        if not 4 <= n <= 2**24:
                            raise ValueError('test frame size')
                        body = exact(backend, n - 4)
                        if dropping.is_set():
                            if tag == b'Z':
                                self.dropped.set()
                                client.shutdown(socket.SHUT_RDWR)
                                return
                        else:
                            client.sendall(tag + size + body)
                except EOFError, OSError, ValueError:
                    pass

            reader = threading.Thread(target=responses, daemon=True)
            self.threads.append(reader)
            reader.start()
            while not self.closed.is_set():
                tag = exact(client, 1)
                size = exact(client, 4)
                n = int.from_bytes(size, 'big')
                if not 4 <= n <= 2**24:
                    raise ValueError('test frame size')
                body = exact(client, n - 4)
                if tag == b'P':
                    statement = body.split(b'\0', 2)[1].lstrip().upper()
                    if statement.startswith((b'INSERT ', b'UPDATE ', b'DELETE ')):
                        wrote.set()
                if tag == b'Q' and body.rstrip(b'\0; \n').upper() == b'COMMIT' and wrote.is_set():
                    dropping.set()
                backend.sendall(tag + size + body)
        except EOFError, OSError, ValueError:
            pass
        finally:
            client.close()
            if backend is not None:
                backend.close()

    def __exit__(self, *_):
        self.closed.set()
        self.listener.close()
        for sock in self.sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for thread in self.threads:
            thread.join(timeout=1)
