import pytest

from msg.cli import parser


def test_wait_modes_are_explicit():
    assert parser().parse_args(['listen']).wait_mode == 'auto'
    assert parser().parse_args(['listen', '--wait-mode', 'poll']).wait_mode == 'poll'
    assert parser().parse_args(['listen', '--wait-mode', 'events']).wait_mode == 'events'


async def test_required_remote_events_fail_without_polling_fallback():
    from types import SimpleNamespace

    from msg.client_changes_wait import ChangesWait
    from msg.core.errors import Failure

    class Client:
        async def call(self, operation, arguments, **options):
            assert operation == 'communication.changes'
            assert options == {'contract_version': 2}
            return SimpleNamespace(data={'items': [], 'has_more': False, 'wait_supported': False})

        def checked(self, result):
            return result

    with pytest.raises(Failure, match='listener_events_unavailable'):
        await ChangesWait(Client(), mode='events').read({})
