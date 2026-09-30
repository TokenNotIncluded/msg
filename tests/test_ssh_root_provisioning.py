"""SSH provisioning opt-in stays separate from the default console boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from msg.admin import root
from msg.core.errors import Failure
from msg.daemon import parser


def setup_terminal(
    monkeypatch, *, uid=0, connection='192.0.2.1 1234 192.0.2.2 22', tty='/dev/pts/3', mode=0o40755
):
    monkeypatch.setattr(root.os, 'geteuid', lambda: uid)
    monkeypatch.delenv('SSH_CONNECTION', raising=False)
    if connection:
        monkeypatch.setenv('SSH_CONNECTION', connection)
    monkeypatch.setattr(root.sys, 'stdin', Mock(isatty=lambda: True, fileno=lambda: 0))
    monkeypatch.setattr(root.sys, 'stdout', Mock(isatty=lambda: True))
    monkeypatch.setattr(root.os, 'ttyname', lambda _: tty)
    path = Mock()
    path.resolve.return_value = path
    path.exists.return_value = True
    path.stat.return_value = SimpleNamespace(st_uid=0, st_mode=mode)
    monkeypatch.setattr(root, 'Path', lambda _: path)


def test_explicit_ssh_provisioning_records_channel(monkeypatch):
    setup_terminal(monkeypatch)
    assert root.require_ssh_administrator('/etc/msgd').startswith('uid:0:ssh:/dev/pts/3:')
    with pytest.raises(Failure, match='remote_admin_forbidden'):
        root.require_local_console('/etc/msgd')


@pytest.mark.parametrize(
    'kwargs,code',
    [
        ({'uid': 1000}, 'local_os_administrator_required'),
        ({'connection': ''}, 'ssh_administrator_required'),
        ({'tty': '/dev/tty1'}, 'ssh_terminal_required'),
        ({'mode': 0o40777}, 'unsafe_config_owner'),
    ],
)
def test_ssh_provisioning_rejects_invalid_context(monkeypatch, kwargs, code):
    setup_terminal(monkeypatch, **kwargs)
    with pytest.raises(Failure, match=code):
        root.require_ssh_administrator('/etc/msgd')


def test_ssh_provisioning_requires_interactive_pin(monkeypatch):
    setup_terminal(monkeypatch)
    monkeypatch.setattr(root.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(Failure, match='interactive_pin_required'):
        root.require_ssh_administrator('/etc/msgd')


def test_flag_only_available_for_init_and_issue():
    assert (
        parser()
        .parse_args(['init', '--service-url', 'https://example.org', '--allow-ssh'])
        .allow_ssh
    )
    assert parser().parse_args(['cert', 'issue', 'csr_test', '--allow-ssh']).allow_ssh
    assert not parser().parse_args(['init', '--service-url', 'https://example.org']).allow_ssh
    with pytest.raises(SystemExit):
        parser().parse_args(['root', 'rotate', '--allow-ssh'])
