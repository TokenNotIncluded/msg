"""Wallet amounts stay exact and transfer commands require identity signatures."""

from argparse import Namespace
from types import SimpleNamespace

import pytest

from msg.client_money import amount_minor, display_amount, run_command
from msg.core.errors import Failure


@pytest.mark.parametrize(
    'value,minor',
    [
        ('1', 1000000),
        ('1.25', 1250000),
        ('0.000001', 1),
        ('9223372036854.775807', 9223372036854775807),
    ],
)
def test_amount_is_exact(value, minor):
    assert amount_minor(value, 6) == minor
    assert amount_minor(display_amount(minor, 6), 6) == minor


@pytest.mark.parametrize(
    'value', ['0', '-1', '1e3', 'NaN', '0.0000001', '9223372036854.775808', '1;echo bad']
)
def test_invalid_amount_rejected(value):
    with pytest.raises(Failure):
        amount_minor(value, 6)


@pytest.mark.asyncio
async def test_transfer_explicitly_uses_signer_and_retry_id():
    calls = []
    signer = object()

    class Client:
        signer_override = None
        state = SimpleNamespace(signer=signer)
        checked = staticmethod(lambda value: value)

        async def call(self, operation, args=None, **kwargs):
            calls.append((operation, args, kwargs))
            data = (
                {'currency_id': 'currency', 'scale': 6, 'code': 'MSG'}
                if operation == 'money.state'
                else {'id': 'u_recipient', 'type': 'user'}
                if operation == 'discovery.get'
                else {'balance_minor': 500000}
            )
            return SimpleNamespace(data=data)

    result = await run_command(
        Client(),
        Namespace(
            action='transfer',
            amount='1.25',
            recipient='@other',
            reference="agent's work",
            request_id='stable',
        ),
    )
    assert calls[-1] == (
        'money.transfer',
        {
            'to_subject': 'u_recipient',
            'currency_id': 'currency',
            'amount_minor': 1250000,
            'reference': "agent's work",
        },
        {'signer': signer, 'request_id': 'stable'},
    )
    assert result['data']['balance'] == '0.5'


@pytest.mark.asyncio
async def test_real_signed_cli_transfer_is_exact_and_retry_safe(installed, tmp_path):
    import httpx
    from test_money_core import _fund_fixture
    from test_service import NOW, call, register

    from msg import cli
    from msg.client import ClientState, MsgClient
    from msg.client_market import run_command as run_market
    from msg.transports.client import HTTPTransport
    from msg.transports.http import create_app

    app, _ = installed
    bob_key, bob, _ = await register(app, 'wallet-cli-bob')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        client = MsgClient(
            ClientState(tmp_path / 'wallet-client', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        registered = await client.register('wallet-cli-alice')
        assert registered.status == 'ok'
        await _fund_fixture(app, client.state.subject, 1000000)
        args = cli.parser().parse_args([
            'money',
            'transfer',
            '@wallet-cli-bob',
            '0.4',
            '--request-id',
            'wallet-repeat-id',
            '--reference',
            'agent task',
        ])
        first = await run_market(client, args, lambda value: {})
        assert first['status'] == 'ok'
        again = await run_market(client, args, lambda value: {})
        assert again == first
        balance = await call(app, 'money.balance', {}, key=bob_key, subject=bob)
        assert balance.data['balance_minor'] == 400000
        own = await client.call('money.balance')
        assert own.data['balance_minor'] == 600000
