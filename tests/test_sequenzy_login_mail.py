"""Mocked official Sequenzy contract and secret/file/HTTP boundaries only."""

import json
import os
import traceback

import httpx
import pytest

from msg.core.errors import Failure
from msg.workers import sequenzy

API_KEY = 'seq_live_fixture_secret'
ADDRESS = 'login-reader@example.test'
CODE = '58172639'


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / 'sequenzy.json'
    path.write_text(json.dumps({'api_key': API_KEY}))
    path.chmod(0o600)
    return path


@pytest.fixture
def transport(monkeypatch):
    real_client = httpx.Client
    requests, options = [], []

    def install(handler):
        def dispatch(request):
            requests.append(request)
            return handler(request)

        def client(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(dispatch), **kwargs)

        monkeypatch.setattr(sequenzy, 'Client', client)
        return requests, options

    return install


def accepted(_):
    return httpx.Response(
        200,
        headers={'content-type': 'application/json'},
        stream=httpx.ByteStream(
            json.dumps({
                'success': True,
                'emailSendId': 'send_fixture',
                'jobId': 'job_fixture',
                'to': ADDRESS,
            }).encode()
        ),
    )


@pytest.mark.parametrize('sender', [None, 'MSG <login@example.test>'])
async def test_exact_transactional_contract(credentials, transport, sender):
    requests, options = transport(accepted)
    sender_service = sequenzy.SequenzySender(credentials, sender)
    assert await sender_service.send_code(ADDRESS, CODE) == 'queued'
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'POST'
    assert str(request.url) == 'https://api.sequenzy.com/api/v1/transactional/send'
    assert not request.url.query
    assert request.headers['authorization'] == 'Bearer ' + API_KEY
    assert request.headers['content-type'] == 'application/json'
    assert request.headers['accept-encoding'] == 'identity'
    payload = json.loads(request.content)
    assert payload['to'] == ADDRESS
    assert payload['subject'] == 'Your MSG login code'
    assert CODE in payload['body']
    assert payload['emailType'] == 'transactional'
    assert payload['trackingSettings'] == {'clickTracking': False, 'openTracking': False}
    assert set(payload) == {'to', 'subject', 'body', 'emailType', 'trackingSettings'} | (
        {'from'} if sender else set()
    )
    assert payload.get('from') == sender
    assert API_KEY not in request.content.decode()
    assert API_KEY not in repr(sender_service)
    assert options[0]['follow_redirects'] is False
    assert options[0]['trust_env'] is False
    assert options[0]['timeout'].connect == 5.0
    assert options[0]['timeout'].read == 10.0
    assert options[0]['timeout'].write == 10.0
    assert options[0]['timeout'].pool == 10.0


def assert_redacted(exc):
    summary = (
        str(exc.value) + repr(exc.value.as_dict()) + ''.join(traceback.format_exception(exc.value))
    )
    assert not exc.value.retryable
    assert exc.value.__cause__ is None
    for secret in (API_KEY, ADDRESS, CODE, 'full-response-marker'):
        assert secret not in summary


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 429, 500, 502])
async def test_status_errors_and_redirects_are_redacted(credentials, transport, status):
    requests, _ = transport(
        lambda _: httpx.Response(
            status,
            headers={'location': 'https://other.example.test/' + API_KEY},
            text=f'full-response-marker {API_KEY} {ADDRESS} {CODE}',
        )
    )
    with pytest.raises(Failure, match='sequenzy_send_failed') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert len(requests) == 1
    assert_redacted(exc)


@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError])
async def test_network_errors_do_not_expose_or_retry(credentials, transport, error):
    def fail(request):
        raise error(f'{API_KEY} {ADDRESS} {CODE} full-response-marker', request=request)

    requests, _ = transport(fail)
    with pytest.raises(Failure, match='sequenzy_send_failed') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert len(requests) == 1
    assert_redacted(exc)


@pytest.mark.parametrize(
    'body',
    [
        b'not JSON',
        b'[]',
        b'{"success":false}',
        b'{"success":"true"}',
        b'{"success":true,"emailType":"marketing"}',
        b'{"success":true,"success":false}',
        (f'{{"{API_KEY}":1,"{API_KEY}":2}}').encode(),
    ],
)
async def test_invalid_success_response_is_redacted(credentials, transport, body):
    transport(lambda _: httpx.Response(200, stream=httpx.ByteStream(body)))
    with pytest.raises(Failure, match='sequenzy_send_failed') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert_redacted(exc)


class CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.read, self.closed = chunks, 0, False

    def __iter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize('declared_size', [False, True])
async def test_response_limit_stops_and_closes_stream(credentials, transport, declared_size):
    stream = CountingStream([b'x' * sequenzy.MAX_RESPONSE_BYTES, b'y', b'never-consumed'])
    headers = {'content-length': str(sequenzy.MAX_RESPONSE_BYTES + 1)} if declared_size else {}
    transport(lambda _: httpx.Response(200, headers=headers, stream=stream))
    with pytest.raises(Failure, match='sequenzy_send_failed') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert stream.read == (0 if declared_size else 2)
    assert stream.closed
    assert_redacted(exc)


async def test_no_implicit_response_decompression(credentials, transport):
    stream = CountingStream([b'not-a-compressed-body'])
    transport(lambda _: httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=stream))
    with pytest.raises(Failure, match='sequenzy_send_failed') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert stream.read == 0
    assert stream.closed
    assert_redacted(exc)


async def test_slow_stream_exceeds_total_budget(credentials, transport, monkeypatch):
    clock = iter([0.0, 0.0, sequenzy.MAX_SEND_SECONDS + 1])
    monkeypatch.setattr(sequenzy, 'monotonic', lambda: next(clock))
    stream = CountingStream([b'{"success":true}', b'never-consumed'])
    transport(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(Failure, match='sequenzy_send_failed') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert stream.read == 1
    assert stream.closed
    assert_redacted(exc)


@pytest.mark.parametrize('mode', [0o400, 0o640, 0o644, 0o620, 0o602, 0o666, 0o700])
async def test_credentials_require_private_file_mode(credentials, transport, mode):
    credentials.chmod(mode)
    requests, _ = transport(accepted)
    with pytest.raises(Failure, match='invalid_sequenzy_credentials') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert not requests
    assert_redacted(exc)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory', 'missing', 'fifo'])
async def test_credentials_reject_unsafe_file_types(credentials, transport, tmp_path, kind):
    target = tmp_path / 'unsafe-credentials'
    if kind == 'symlink':
        target.symlink_to(credentials)
    elif kind == 'hardlink':
        os.link(credentials, target)
    elif kind == 'directory':
        target.mkdir(mode=0o700)
    elif kind == 'fifo':
        os.mkfifo(target, 0o600)
    requests, _ = transport(accepted)
    with pytest.raises(Failure, match='invalid_sequenzy_credentials') as exc:
        await sequenzy.SequenzySender(target).send_code(ADDRESS, CODE)
    assert not requests
    assert_redacted(exc)


async def test_credentials_require_current_os_owner(credentials, transport, monkeypatch):
    from msg.security import root_files

    current = os.geteuid()
    monkeypatch.setattr(root_files.os, 'geteuid', lambda: current + 1)
    requests, _ = transport(accepted)
    with pytest.raises(Failure, match='invalid_sequenzy_credentials') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert not requests
    assert_redacted(exc)


@pytest.mark.parametrize(
    'raw',
    [
        '',
        'invalid-json-' + API_KEY,
        '[]',
        '{}',
        json.dumps({'api_key': API_KEY, API_KEY: 'unexpected'}),
        json.dumps({'api_key': None}),
        json.dumps({'api_key': 123}),
        json.dumps({'api_key': ''}),
        json.dumps({'api_key': 'seq_user_fixture_secret'}),
        json.dumps({'api_key': API_KEY + '\r\nInjected: header'}),
        json.dumps({'api_key': 'https://example.test/?key=' + API_KEY}),
        json.dumps({'api_key': 'x' * 4097}),
        '{"api_key":"a","api_key":"b"}',
        'x' * (sequenzy.MAX_CREDENTIAL_BYTES + 1),
    ],
)
async def test_credentials_reject_invalid_json_shape_and_key(credentials, transport, raw):
    credentials.write_text(raw)
    requests, _ = transport(accepted)
    with pytest.raises(Failure, match='invalid_sequenzy_credentials') as exc:
        await sequenzy.SequenzySender(credentials).send_code(ADDRESS, CODE)
    assert not requests
    assert_redacted(exc)


@pytest.mark.parametrize('address', ['invalid', 'a@example.test,b@example.test', ADDRESS + '\n'])
async def test_bad_recipient_fails_before_credentials_or_network(transport, tmp_path, address):
    requests, _ = transport(accepted)
    with pytest.raises(Failure, match='invalid_email'):
        await sequenzy.SequenzySender(tmp_path / 'missing').send_code(address, CODE)
    assert not requests


@pytest.mark.parametrize('code', ['', '<b>123456</b>', '123456\n', 123456, 'a' * 129])
async def test_bad_code_fails_before_credentials_or_network(transport, tmp_path, code):
    requests, _ = transport(accepted)
    with pytest.raises(Failure, match='invalid_login_code'):
        await sequenzy.SequenzySender(tmp_path / 'missing').send_code(ADDRESS, code)
    assert not requests


@pytest.mark.parametrize('sender', ['', 'invalid', 'a@example.test,b@example.test', ADDRESS + '\n'])
def test_invalid_sender_fails_locally(credentials, sender):
    with pytest.raises(Failure, match='invalid_sequenzy_sender'):
        sequenzy.SequenzySender(credentials, sender)
