from types import SimpleNamespace

import pytest

from msg.client_agent_cli import local_command
from msg.client_subscription import Subscription
from msg.core.errors import Failure


class Client:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    async def call(self, operation, args, **kwargs):
        self.calls.append((operation, args, kwargs))
        return SimpleNamespace(data=next(self.pages))

    @staticmethod
    def checked(result):
        return result


async def test_subscription_uses_explicit_version_and_persists_server_cursor():
    client = Client([{'items': [], 'cursor': 'next', 'has_more': False, 'wait_supported': True}])
    source = Subscription(client, resource='r_private')
    page = await source.fetch('saved')
    assert page['cursor'] == 'next'
    operation, args, options = client.calls[0]
    assert operation == 'communication.events' and options['contract_version'] == 2
    assert args == {'resource': 'r_private', 'cursor': 'saved', 'wait_ms': 20000, 'limit': 50}


async def test_pending_revalidation_returns_new_projection_not_cached_body():
    current = {'id': 'e_done', 'seq': 10, 'resources': []}
    client = Client([
        {'items': [], 'cursor': 'scan', 'has_more': True},
        {'items': [current], 'cursor': 'end', 'has_more': False},
    ])
    source = Subscription(client)
    refreshed = await source.revalidate({'id': 'e_done', 'seq': 10, 'before_cursor': 'before'})
    assert refreshed == current
    assert [args['cursor'] for _, args, _ in client.calls] == ['before', 'scan']
    assert all(args['wait_ms'] == 0 for _, args, _ in client.calls)


async def test_pending_revocation_drops_cached_event():
    client = Client([{'items': [{'id': 'e_later', 'seq': 11}], 'cursor': 'end', 'has_more': True}])
    assert (
        await Subscription(client).revalidate({
            'id': 'e_done',
            'seq': 10,
            'before_cursor': 'before',
            'secret': 'stale',
        })
        is None
    )


def test_regular_read_resource_does_not_request_subscription():
    args = SimpleNamespace(command='read', resource='/main', agent=None, remote=False)
    assert local_command(args) is False


def test_listen_resource_cannot_silently_route_to_local():
    args = SimpleNamespace(command='listen', resource='/main', agent='worker', remote=False)
    with pytest.raises(Failure, match='subscription_required'):
        local_command(args)
