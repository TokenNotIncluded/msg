"""Public helper ownership, unchanged bytes and supported client/server installs."""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_result_serialization_has_one_protocol_owner():
    from msg.core.codec import result_wire
    from msg.core.executor import result_wire as compatibility_export
    from msg.core.models import OperationResult
    from msg.cli import result_wire as client_export

    assert result_wire is compatibility_export is client_export
    assert result_wire.__module__ == 'msg.core.codec'
    result = OperationResult(request_id='r', operation='content.read', status='ok', actor=None,
                             subject=None, resources=(), data={'text': 'plain'})
    value = result_wire(result)
    assert value['data'] == {'text': 'plain'}
    assert 'replayed' not in value and 'prefer_cli' not in value


def test_atomic_file_owner_keeps_compatibility_and_failed_replace_protection(tmp_path, monkeypatch):
    from msg.atomic_file import durable_write
    from msg.storage.git import durable_write as compatibility_export
    import msg.atomic_file as atomic

    assert durable_write is compatibility_export
    path = tmp_path / 'state' / 'identity'
    durable_write(path, b'first')
    assert path.read_bytes() == b'first' and path.stat().st_mode & 0o777 == 0o600
    def fail_replace(*args):
        raise OSError('simulated replace refusal')
    monkeypatch.setattr(atomic.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='simulated replace refusal'):
        durable_write(path, b'not committed')
    assert path.read_bytes() == b'first'
    assert list(path.parent.iterdir()) == [path]


def test_custodial_proof_and_statements_are_shared_without_vault_state():
    from msg.security.custodial_protocol import (
        UPGRADE_CONTEXT_FIELDS, _proof, client_upgrade_proof, stage_statement, ack_statement)
    from msg.security.vault import client_upgrade_proof as old_proof
    from msg.security.custodial_migration import stage_statement as old_stage, ack_statement as old_ack
    from msg.core.codec import b64, canonical
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    assert client_upgrade_proof is old_proof
    assert stage_statement is old_stage and ack_statement is old_ack
    assert client_upgrade_proof.__module__ == 'msg.security.custodial_protocol'
    expected_fields = ('subject_id', 'challenge_id', 'request_id', 'handle', 'public_key',
                       'encryption_recipient', 'server_public', 'nonce', 'expires_at')
    assert UPGRADE_CONTEXT_FIELDS == expected_fields
    challenge = {name: name + '-fixture' for name in expected_fields}
    challenge['nonce'] = b64(bytes(range(32)))
    challenge['not_signed'] = 'not part of this published proof context'
    context = {name: challenge[name] for name in expected_fields}
    shared = bytes(range(32, 64))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=bytes(range(32)),
               info=b'msg-custodial-upgrade-pop-v1').derive(shared)
    expected = b64(hmac.digest(key, canonical(context), 'sha256'))
    assert _proof(shared, challenge) == expected
    assert stage_statement('u', {'choice': 'rewrap', 'migration_ack': 'omit'}, 'r') == {
        'domain': 'msg-custodial-decision-v1', 'subject_id': 'u', 'request_id': 'r',
        'decision': {'choice': 'rewrap'}}
    assert ack_statement('u', 'c', 'd', {'id': 'old'}, {'id': 'new'}, 'p', 'r') == {
        'domain': 'msg-custodial-history-ack-v1', 'subject_id': 'u', 'challenge_id': 'c',
        'inventory_digest': 'd', 'source': {'id': 'old'}, 'target': {'id': 'new'},
        'method': 'rewrap', 'plaintext_digest': 'p', 'request_id': 'r'}


def test_mcp_client_uses_neutral_version_declaration():
    from msg.transports.mcp_protocol import PROTOCOL_VERSION, SUPPORTED_VERSIONS
    from msg.transports import mcp
    assert PROTOCOL_VERSION == mcp.PROTOCOL_VERSION == '2025-11-25'
    assert SUPPORTED_VERSIONS is mcp.SUPPORTED_VERSIONS
    assert SUPPORTED_VERSIONS == frozenset({'2025-11-25', '2025-06-18', '2025-03-26'})


def test_fresh_client_import_guard_covers_actual_four_transport_calls():
    completed = subprocess.run([sys.executable, str(ROOT / 'scripts/check_client_install.py')],
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    import json
    report = json.loads(completed.stdout)
    assert report['transports'] == ['graphql', 'http', 'mcp_http', 'path_get']
    assert report['server_implementation_imported'] is False


def test_install_metadata_separates_roles_but_dev_keeps_full_server():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    def name(requirement):
        return requirement.split('>=')[0].split('[')[0]
    server_names = {'starlette', 'uvicorn', 'psycopg', 'valkey', 'aiohttp', 'dnspython', 'graphql-core'}
    assert not server_names & {name(value) for value in project['dependencies']}
    extras = project['optional-dependencies']
    assert server_names <= {name(value) for value in extras['server']}
    assert 'msg-lmm-best[server]' in extras['dev']
    assert project['scripts'] == {'msg': 'msg.cli:main', 'msgd': 'msg.daemon:main'}


def test_missing_server_dependencies_are_reported_before_local_state(tmp_path, monkeypatch, capsys):
    import msg.daemon as daemon
    original = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec',
        lambda name, *args, **kwargs: None if name == 'psycopg' else original(name, *args, **kwargs))
    directory = tmp_path / 'not-created'
    assert daemon.main(['--config-dir', str(directory), 'serve']) == 2
    captured = capsys.readouterr()
    assert 'server_dependencies_required' in captured.err
    assert 'msg-lmm-best[server]' in captured.err
    assert not directory.exists()
