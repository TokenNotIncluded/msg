"""Process names and root-owned interpreters are not an SSH authentication channel."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from msg.core.errors import Failure
from msg.extensions import ssh


@pytest.mark.parametrize('name', ['sshd', 'sshd-session', 'sshd-auth'])
def test_real_unprivileged_process_cannot_spoof_an_sshd_ancestor(name):
    # Linux PR_SET_NAME is available to any process. Its Python executable is
    # root-owned, exactly like most ordinary system commands. Neither proves
    # that the child was authenticated by the system's OpenSSH service.
    probe = (
        'from msg.extensions.ssh import require_sshd_process\n'
        'from msg.core.errors import Failure\n'
        'try: require_sshd_process()\n'
        'except Failure as exc: print(exc.code)\n'
        'else: raise SystemExit("spoofed sshd accepted")\n'
    )
    parent = (
        'import ctypes, subprocess, sys\n'
        f'assert ctypes.CDLL(None).prctl(15, {name.encode()!r}, 0, 0, 0) == 0\n'
        f'raise SystemExit(subprocess.run([sys.executable, "-c", {probe!r}]).returncode)\n'
    )
    # Root-driven local runs must exercise an unprivileged attacker too. CI
    # normally already runs as an unprivileged account; no service/user changes.
    identity = {'user': 65534, 'group': 65534, 'extra_groups': []} if os.geteuid() == 0 else {}
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    result = subprocess.run(
        [sys.executable, '-c', parent],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        **identity,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'ssh_os_isolation_required'


def proc_tree(monkeypatch, tmp_path, *, owner, real_uid, effective_uid, parent=1):
    directory = tmp_path / '123'
    directory.mkdir()
    (directory / 'comm').write_text('sshd\n')
    (directory / 'status').write_text(
        f'Uid:\t{real_uid}\t{effective_uid}\t{effective_uid}\t{effective_uid}\nPPid:\t{parent}\n'
    )
    monkeypatch.setattr(ssh, 'Path', lambda root: tmp_path)
    monkeypatch.setattr(ssh.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(ssh.os, 'getppid', lambda: 123)
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path == directory:
            return SimpleNamespace(st_uid=owner)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stat)
    return directory


def test_privileged_sshd_ancestor_does_not_require_readable_proc_exe(monkeypatch, tmp_path):
    proc_tree(monkeypatch, tmp_path, owner=0, real_uid=0, effective_uid=0)
    ssh.require_sshd_process()


@pytest.mark.parametrize('owner,real_uid,effective_uid', [(1000, 0, 0), (0, 1000, 0), (0, 0, 1000)])
def test_inconsistent_or_unprivileged_sshd_identity_fails_closed(
    monkeypatch, tmp_path, owner, real_uid, effective_uid
):
    proc_tree(monkeypatch, tmp_path, owner=owner, real_uid=real_uid, effective_uid=effective_uid)
    with pytest.raises(Failure, match='ssh_os_isolation_required'):
        ssh.require_sshd_process()


def test_root_caller_is_not_a_permitted_application_ssh_session(monkeypatch):
    monkeypatch.setattr(ssh.os, 'geteuid', lambda: 0)
    with pytest.raises(Failure, match='ssh_os_isolation_required'):
        ssh.require_sshd_process()
