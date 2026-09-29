"""Invalid local chunk sizes cannot touch resumable state or cause zero progress."""
import hashlib

import httpx
import pytest

from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.core.errors import Failure
from msg.transports.client import (
    GraphQLTransport, HTTPTransport, MCPHTTPTransport, PathGETTransport,
)
from msg.transports.http import create_app
from read_only_evidence import readonly_evidence
from test_service import NOW


INVALID_PART_BYTES = (0, -1, False, True, 1.5, '1', None)
TRANSPORTS = (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport)


class InterruptedDownload(Exception):
    pass


def local_snapshot(directory, partial):
    paths = sorted(p for p in directory.rglob('*') if p.is_file())
    if partial.exists():
        paths.append(partial)
    # Hash fixture keys rather than printing their contents in assertion diffs.
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(),
                      p.stat().st_mode, p.stat().st_mtime_ns) for p in paths}


async def start_interrupted_download(app, tmp_path, http, monkeypatch):
    state = ClientState(tmp_path / 'client', server=app.settings.service_url)
    transport = HTTPTransport(app.settings.service_url, http=http)
    client = MsgClient(state, transport, clock=lambda: NOW)
    registered = await client.register('part-size-agent')
    assert registered.status == 'ok'
    source = tmp_path / 'source.bin'
    source.write_bytes(b'resume\x00exact bytes\n')
    sealed = await client.upload(source, part_bytes=7)
    destination = tmp_path / 'destination.bin'
    actual_call, parts = transport.call, 0

    async def interrupt_second_part(packet):
        nonlocal parts
        if packet.operation == 'transfer.part_get':
            parts += 1
            if parts == 2:
                raise InterruptedDownload()
        return await actual_call(packet)

    with monkeypatch.context() as patch:
        patch.setattr(transport, 'call', interrupt_second_part)
        with pytest.raises(InterruptedDownload):
            await client.download(sealed.output, destination, part_bytes=3)
    partial = destination.with_name(destination.name + '.msg-part')
    assert partial.read_bytes() == source.read_bytes()[:3]
    journals = list(state.directory.glob('download-*.json'))
    assert len(journals) == 1 and loads(journals[0].read_bytes())['offset'] == 3
    assert not destination.exists()
    return client, sealed.output, source, destination, partial


@pytest.mark.parametrize('part_bytes', INVALID_PART_BYTES)
async def test_resume_rejects_invalid_local_chunk_size_before_state_or_network(
        installed, tmp_path, monkeypatch, part_bytes):
    app, _ = installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        client, ref, _, destination, partial = await start_interrupted_download(
            app, tmp_path, http, monkeypatch)
        before = local_snapshot(client.state.directory, partial)
        actual_call, requests = client.transport.call, []

        async def bounded_real_calls(packet):
            requests.append((packet.operation, dict(packet.arguments)))
            # Let the original bug perform two actual signed zero-byte reads,
            # then stop deterministically instead of hanging the CI worker.
            if len(requests) > 2:
                raise AssertionError(f'non-progressing download requests: {requests}')
            return await actual_call(packet)

        with monkeypatch.context() as patch:
            patch.setattr(client.transport, 'call', bounded_real_calls)
            async with readonly_evidence(app, monkeypatch):
                with pytest.raises(Failure, match='^invalid_part_bytes$'):
                    await client.download(ref, destination, part_bytes=part_bytes)
        assert requests == []
        assert local_snapshot(client.state.directory, partial) == before
        assert not destination.exists()


@pytest.mark.parametrize('part_bytes', INVALID_PART_BYTES)
async def test_upload_rejects_invalid_local_chunk_size_before_source_access(tmp_path, part_bytes):
    def unexpected_network(request):
        raise AssertionError('invalid local arguments reached the network')

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_network)) as http:
        state = ClientState(tmp_path / 'client', server='http://testserver')
        client = MsgClient(state, HTTPTransport(state.server, http=http), clock=lambda: NOW)
        missing_source = tmp_path / 'does-not-exist.bin'
        before = local_snapshot(state.directory, missing_source)
        with pytest.raises(Failure, match='^invalid_part_bytes$'):
            await client.upload(missing_source, part_bytes=part_bytes)
        assert local_snapshot(state.directory, missing_source) == before
        assert not missing_source.exists()


@pytest.mark.parametrize('transport_class', TRANSPORTS)
async def test_download_resume_with_valid_chunk_size_preserves_real_bytes(
        installed, tmp_path, monkeypatch, transport_class):
    app, _ = installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        client, ref, source, destination, partial = await start_interrupted_download(
            app, tmp_path, http, monkeypatch)
        resumed = MsgClient(client.state, transport_class(app.settings.service_url, http=http),
                            clock=lambda: NOW)
        async with readonly_evidence(app, monkeypatch):
            result = await resumed.download(ref, destination, part_bytes=1)
        assert destination.read_bytes() == source.read_bytes()
        assert result['size'] == source.stat().st_size
        assert not partial.exists()
        assert not list(client.state.directory.glob('download-*.json'))
