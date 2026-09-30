"""Aliases change ingress, while keys, certificates and packets keep one authority."""

import argparse
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, register

from msg import cli
from msg.client import ClientState, MsgClient
from msg.client_connection import expand_connection_args
from msg.config import load_settings, write_example
from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.transports.client import TRANSPORTS, HTTPTransport
from msg.transports.http import create_app

ALIAS = 'http://alias.example.org:8042'


def test_alias_configuration_is_explicit_and_does_not_change_authority(tmp_path):
    directory = tmp_path / 'etc'
    write_example(directory, tmp_path / 'data', 'https://primary.example.org')
    path = directory / 'msgd.toml'
    original = path.read_text()
    assert load_settings(directory).service_aliases == ()
    path.write_text(
        original.replace(
            '[server]', '[server]\nservice_aliases = ["https://ALIAS.example.org:443/"]'
        )
    )
    settings = load_settings(directory)
    assert settings.service_aliases == ('https://alias.example.org',)
    assert settings.service_url == 'https://primary.example.org'


@pytest.mark.parametrize(
    'value',
    [
        '"https://alias.example.org"',
        '[1]',
        '["http://alias.example.org"]',
        '["https://alias.example.org/path"]',
        '["https://name:secret@alias.example.org"]',
        '["https://primary.example.org:443"]',
        '["https://alias.example.org", "https://ALIAS.example.org:443/"]',
        '[' + ','.join('"https://alias' + str(i) + '.example.org"' for i in range(33)) + ']',
    ],
)
def test_invalid_alias_configuration_fails_closed(tmp_path, value):
    directory = tmp_path / 'etc'
    write_example(directory, tmp_path / 'data', 'https://primary.example.org')
    path = directory / 'msgd.toml'
    path.write_text(path.read_text().replace('[server]', '[server]\nservice_aliases = ' + value))
    with pytest.raises(Failure, match='^invalid_service_aliases$'):
        load_settings(directory)


@pytest.mark.asyncio
async def test_allowlisted_alias_keeps_canonical_packets_and_origin_boundary(installed):
    app, _ = installed
    key, subject, certificate = await register(app, 'alias-agent')
    app.settings = replace(app.settings, service_aliases=(ALIAS,))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app))) as http:
        description = await http.get(ALIAS + '/_transports')
        assert description.status_code == 200, description.text
        assert description.json()['target_service'] == app.settings.service_url
        packet = request_for(
            'content.post_create',
            {'parent': '/main', 'body': 'one authority'},
            app.settings.service_url,
            signer=key,
            subject=subject,
            certificates=(certificate,),
            expires_at=NOW + timedelta(seconds=90),
            request_id='canonical-alias-post',
        )
        posted = await http.post(
            ALIAS + '/-/p/content.post_create', content=canonical(packet), headers={'Origin': ALIAS}
        )
        assert posted.status_code == 200 and posted.json()['status'] == 'ok', posted.text
        replay = await http.post(
            app.settings.service_url + '/-/p/content.post_create', content=canonical(packet)
        )
        assert replay.json()['replayed'] is True
        wrong = request_for(
            'content.post_create',
            {'parent': '/main', 'body': 'wrong authority'},
            ALIAS,
            signer=key,
            subject=subject,
            certificates=(certificate,),
            expires_at=NOW + timedelta(seconds=90),
        )
        denied = await http.post(ALIAS + '/-/p/content.post_create', content=canonical(wrong))
        assert denied.json()['error']['code'] == 'wrong_service'
        for origin in (app.settings.service_url, 'http://unknown.example.org', 'null'):
            response = await http.post(
                ALIAS + '/-/p/content.post_create',
                content=canonical(packet),
                headers={'Origin': origin},
            )
            assert response.status_code == 403
            assert response.json()['error']['code'] == 'forbidden_origin'
        for headers in (
            {'Host': 'unknown.example.org:8042'},
            {'Host': 'alias.example.org'},
            [('Host', 'alias.example.org:8042'), ('Host', 'testserver')],
        ):
            response = await http.get(ALIAS + '/healthz', headers=headers)
            assert response.status_code == 403
            assert response.json()['error']['code'] == 'forbidden_host'
        app.settings = replace(app.settings, service_aliases=())
        denied = await http.get(ALIAS + '/healthz')
        assert denied.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('transport_type', TRANSPORTS.values())
async def test_all_transports_use_alias_endpoint_and_original_identity(
    installed, tmp_path, transport_type
):
    app, _ = installed
    app.settings = replace(app.settings, service_aliases=(ALIAS,))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app))) as http:
        state = ClientState(tmp_path / 'client', server=app.settings.service_url)
        client = MsgClient(state, HTTPTransport(state.server, http=http), clock=lambda: NOW)
        assert (await client.register('alias-client')).status == 'ok'
        original = state.key_path.read_bytes()
        transport = transport_type(state.server, endpoint=ALIAS, http=http)
        via_alias = MsgClient(state, transport, clock=lambda: NOW)
        assert (await via_alias.call('discovery.get', {'id': '/main'})).status == 'ok'
        await via_alias.require_username('alias-client')
        packet = via_alias.prepare(
            'content.post_create', {'parent': '/main', 'body': 'alias client'}
        )
        assert packet.target_service == state.server
        assert (await via_alias.send(packet)).status == 'ok'
        assert state.key_path.read_bytes() == original
        assert transport.endpoint == ALIAS and transport.server == state.server


def test_ssh_config_maps_network_alias_to_canonical_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    config = tmp_path / 'config'
    config.write_text(
        'Host backup\n HostName alias.example.org\n ServiceURL https://primary.example.org\n User alice\n'
    )
    config.chmod(0o600)
    parser = cli.parser()
    commands = next(a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    args = parser.parse_args(
        expand_connection_args(['-F', str(config), 'backup', 'identity show'], commands)
    )
    assert args.server == 'https://primary.example.org'
    assert args.endpoint == 'https://alias.example.org'
    assert args.user == 'alice'
    ordinary = [
        '--server',
        'https://primary.example.org',
        '--endpoint',
        'https://alias.example.org',
        'identity',
        'show',
    ]
    assert expand_connection_args(ordinary, commands) == ordinary


def test_endpoint_cannot_downgrade_secret_delivery():
    with pytest.raises(Failure, match='^endpoint_scheme_mismatch$'):
        HTTPTransport('https://primary.example.org', endpoint='http://alias.example.org')


@pytest.mark.asyncio
async def test_oauth_security_uses_actual_endpoint_not_canonical_loopback(tmp_path):
    from msg.client_oauth import secure_transport

    state = ClientState(tmp_path / 'client', server='http://testserver')
    transport = HTTPTransport(state.server, endpoint=ALIAS)
    client = MsgClient(state, transport)
    try:
        with pytest.raises(Failure, match='^secure_channel_required$'):
            secure_transport(client)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_oauth_alias_rejects_foreign_authority_before_sending_secret(tmp_path):
    from msg.client_oauth import endpoint

    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={'version': 1, 'target_service': 'https://other.example.org', 'operations': {}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        state = ClientState(tmp_path / 'client', server='https://primary.example.org')
        client = MsgClient(
            state, HTTPTransport(state.server, endpoint='https://alias.example.org', http=http)
        )
        with pytest.raises(Failure, match='^service_mismatch$'):
            await endpoint(client, '/oauth/token', {'refresh_token': 'must-not-send'})
    assert len(calls) == 1 and calls[0].method == 'GET' and not calls[0].content


@pytest.mark.asyncio
async def test_alias_description_cannot_rebind_identity_or_follow_redirect():
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json={'version': 1, 'target_service': ALIAS, 'operations': {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        transport = HTTPTransport('http://primary.example.org', endpoint=ALIAS, http=http)
        with pytest.raises(Failure, match='^service_mismatch$'):
            await transport.description()
    assert len(calls) == 1 and str(calls[0].url) == ALIAS + '/_transports'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                308, headers={'Location': 'http://primary.example.org/_transports'}
            )
        )
    ) as http:
        transport = HTTPTransport('http://primary.example.org', endpoint=ALIAS, http=http)
        with pytest.raises(Failure, match='^redirect_not_allowed$'):
            await transport.description()
