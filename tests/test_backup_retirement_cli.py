"""Installed administrative entry and real descriptor boundaries, not console success."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from msg.core.errors import Failure
from msg.daemon import parser


@pytest.mark.parametrize('action', ['sign', 'import'])
def test_retirement_command_parser(action, tmp_path):
    command = ['root', 'backup-retirement', action, str(tmp_path/'statement.json')]
    if action == 'sign':
        command.append(str(tmp_path/'record.json'))
    args = parser().parse_args(command)
    assert args.retirement_command == action
    assert args.source == tmp_path/'statement.json'


@pytest.mark.parametrize('action', ['sign', 'import'])
def test_retirement_cli_refuses_nonconsole_before_reading_files(action, tmp_path):
    command = [str(Path(sys.executable).with_name('msgd')),
               '--config-dir', str(tmp_path), 'root', 'backup-retirement', action,
               str(tmp_path/'missing.json')]
    if action == 'sign':
        command.append(str(tmp_path/'output.json'))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    for key in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY'):
        env.pop(key, None)
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 1
    expected = 'local_os_administrator_required' if os.geteuid() else 'local_console_required'
    assert json.loads(result.stderr) == {'status': 'error', 'error': {'code': expected}}
    assert result.stdout == ''
    assert not (tmp_path/'output.json').exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'permissions', 'fifo', 'oversize'])
def test_statement_file_boundary(kind, tmp_path):
    from msg.admin.backup_retirement import read_statement
    path = tmp_path/'statement.json'
    path.write_bytes(b'{}')
    path.chmod(0o600)
    if kind == 'symlink':
        target = tmp_path/'target'
        path.rename(target)
        path.symlink_to(target)
    elif kind == 'hardlink':
        os.link(path, tmp_path/'other')
    elif kind == 'permissions':
        path.chmod(0o644)
    elif kind == 'fifo':
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(b' ' * (1024 * 1024 + 1))
    with pytest.raises(Failure, match='unsafe_root_private_file'):
        read_statement(path)


def test_statement_file_valid(tmp_path):
    from msg.admin.backup_retirement import read_statement
    path = tmp_path/'statement.json'
    path.write_bytes(b'{"statement": {}}')
    path.chmod(0o600)
    assert read_statement(path) == {'statement': {}}


@pytest.mark.parametrize('existing', [False, True])
def test_record_output_never_overwrites(existing, tmp_path):
    from msg.admin.backup_retirement import write_record
    target = tmp_path/'record.json'
    if existing:
        target.write_bytes(b'preserve')
        with pytest.raises(Failure, match='backup_retirement_destination_exists'):
            write_record(target, {'statement': {}})
        assert target.read_bytes() == b'preserve'
    else:
        write_record(target, {'statement': {}})
        assert json.loads(target.read_bytes()) == {'statement': {}}
        assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.retirement-*'))


def test_record_output_refuses_symlink(tmp_path):
    from msg.admin.backup_retirement import write_record
    victim = tmp_path/'victim.json'
    victim.write_bytes(b'preserve')
    target = tmp_path/'record.json'
    target.symlink_to(victim)
    with pytest.raises(Failure, match='backup_retirement_destination_exists'):
        write_record(target, {'statement': {}})
    assert target.is_symlink()
    assert victim.read_bytes() == b'preserve'
    assert not list(tmp_path.glob('.retirement-*'))
