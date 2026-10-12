import pytest

from msg.cli import parser


def test_wait_modes_are_explicit():
    assert parser().parse_args(['listen']).wait_mode == 'auto'
    assert parser().parse_args(['listen', '--wait-mode', 'poll']).wait_mode == 'poll'
    assert parser().parse_args(['listen', '--wait-mode', 'events']).wait_mode == 'events'


async def test_remote_events_fail_before_any_network_call():
    from types import SimpleNamespace

    from msg.client_agent_cli import run_remote
    from msg.core.errors import Failure

    with pytest.raises(Failure, match='listener_events_unavailable'):
        await run_remote(None, SimpleNamespace(command='listen', offline=False, wait_mode='events'))
