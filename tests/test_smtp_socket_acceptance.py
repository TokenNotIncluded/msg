"""Exercise SMTP over a real, certificate-verified loopback TLS socket."""

import socket
import ssl
import subprocess
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from threading import Thread
from types import SimpleNamespace

import pytest

from msg.core.errors import Failure
from msg.workers.mail import SmtpSender


@pytest.fixture
def tls_contexts(tmp_path):
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
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
            '/CN=localhost',
            '-addext',
            'subjectAltName=IP:127.0.0.1',
        ],
        check=True,
        capture_output=True,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    return server, client


@contextmanager
def smtp_sink(context, outcome):
    commands, messages, errors = [], [], []
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            try:
                conn, _ = listener.accept()
                with context.wrap_socket(conn, server_side=True) as conn:
                    conn.settimeout(5)
                    with conn.makefile('rwb') as wire:

                        def reply(value):
                            wire.write(value + b'\r\n')
                            wire.flush()

                        reply(b'220 localhost isolated test sink')
                        while line := wire.readline():
                            commands.append(line.strip())
                            verb = line.split(b' ', 1)[0].strip().upper()
                            if verb in (b'EHLO', b'HELO'):
                                reply(b'250 localhost')
                            elif verb == b'MAIL':
                                reply(b'250 sender accepted')
                            elif verb == b'RCPT':
                                reply(
                                    b'550 recipient refused'
                                    if outcome == 'refused'
                                    else b'250 recipient accepted'
                                )
                            elif verb == b'RSET':
                                reply(b'250 reset')
                            elif verb == b'DATA':
                                reply(b'354 send message')
                                body = []
                                while (line := wire.readline()) != b'.\r\n':
                                    if not line:
                                        raise AssertionError('EOF during DATA')
                                    body.append(line[1:] if line.startswith(b'..') else line)
                                messages.append(b''.join(body))
                                if outcome == 'disconnect':
                                    return
                                reply(b'250 accepted')
                            else:
                                raise AssertionError(f'unexpected command {verb!r}')
            except (OSError, AssertionError) as exc:
                errors.append(exc)

        thread = Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1], commands, messages
        finally:
            thread.join(6)
            assert not thread.is_alive(), 'SMTP sink did not finish'
            assert not errors, (errors, commands)


def sender(port):
    return SmtpSender(
        SimpleNamespace(
            enabled=True,
            host='127.0.0.1',
            port=port,
            sender='sender@example.test',
            tls='tls',
            credential_file=None,
        )
    )


def job():
    return SimpleNamespace(
        id='isolated-smtp-acceptance',
        arguments={
            'recipient': 'reader@example.test',
            'subject': 'minimal notice',
            'text': 'Open your inbox.\n.dot-stuffed line',
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('outcome', 'expected'),
    [('accepted', 'sent'), ('disconnect', 'uncertain'), ('refused', 'uncertain')],
)
async def test_real_tls_smtp_success_refusal_and_lost_ack(
    tls_contexts, monkeypatch, outcome, expected
):
    server, client = tls_contexts
    monkeypatch.setattr(ssl, 'create_default_context', lambda: client)
    # Keep EHLO hostname discovery independent of the host machine's DNS.
    monkeypatch.setattr(socket, 'getfqdn', lambda: 'sink.example.test')
    with smtp_sink(server, outcome) as (port, commands, messages):
        assert await sender(port).send(job()) == expected
    assert sum(command.upper().startswith(b'MAIL ') for command in commands) == 1
    assert [command.lower() for command in commands if command.upper().startswith(b'RCPT ')] == [
        b'rcpt to:<reader@example.test>'
    ]
    assert len(messages) == (0 if outcome == 'refused' else 1)
    if messages:
        message = BytesParser(policy=policy.default).parsebytes(messages[0])
        assert message['To'] == 'reader@example.test'
        assert message['Message-ID'] == '<isolated-smtp-acceptance@msgd.local>'
        assert '.dot-stuffed line' in message.get_content()


@pytest.mark.asyncio
async def test_real_smtp_connection_failure_is_retryable_before_data():
    # Bind without listening to reserve a port which refuses TCP connections.
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        with pytest.raises(Failure) as caught:
            await sender(reserved.getsockname()[1]).send(job())
    assert caught.value.code == 'mail_connection_failed'
    assert caught.value.retryable
