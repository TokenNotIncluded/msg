"""Local evidence belongs to actual uncommitted files, not an old commit."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('verify_local', ROOT / 'scripts/verify_local.py')
verification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verification)


def test_fingerprint_is_order_independent_and_covers_new_files(tmp_path):
    (tmp_path / 'one.py').write_text('a = 1\n')
    (tmp_path / 'two.py').write_text('b = 2\n')
    first = verification.content_fingerprint(tmp_path, ['one.py', 'two.py'])
    assert first == verification.content_fingerprint(tmp_path, ['two.py', 'one.py', 'one.py'])
    assert first['sha256'] != verification.content_fingerprint(tmp_path, ['one.py'])['sha256']


def test_edit_deletion_and_rename_change_fingerprint(tmp_path):
    path = tmp_path / 'source.py'
    path.write_text('a = 1\n')
    before = verification.content_fingerprint(tmp_path, ['source.py'])
    path.write_text('a = 2\n')
    after = verification.content_fingerprint(tmp_path, ['source.py'])
    with pytest.raises(ValueError, match='source changed'):
        verification.assert_same_source(before, after)
    path.rename(tmp_path / 'renamed.py')
    renamed = verification.content_fingerprint(tmp_path, ['source.py', 'renamed.py'])
    assert renamed['sha256'] != after['sha256']
    assert [entry['path'] for entry in renamed['files']] == ['renamed.py']


def test_symlink_target_is_recorded_without_reading_target(tmp_path):
    (tmp_path / 'outside').write_text('not source\n')
    (tmp_path / 'link').symlink_to('outside')
    before = verification.content_fingerprint(tmp_path, ['link'])
    (tmp_path / 'outside').write_text('changed external contents\n')
    assert verification.content_fingerprint(tmp_path, ['link']) == before
    assert before['files'][0]['kind'] == 'symlink'


@pytest.mark.parametrize('name', ['../outside.py', '/outside.py'])
def test_fingerprint_rejects_paths_outside_checkout(tmp_path, name):
    with pytest.raises(ValueError, match='invalid source path'):
        verification.content_fingerprint(tmp_path, [name])


def test_empty_source_cannot_be_reported_as_verified(tmp_path):
    with pytest.raises(ValueError, match='empty source inventory'):
        verification.content_fingerprint(tmp_path, [])


def test_nonzero_exit_and_stderr_cannot_become_success(tmp_path):
    failed = verification.run_step(
        'exit', [sys.executable, '-c', 'raise SystemExit(3)'], tmp_path, cwd=tmp_path,
    )
    assert failed['status'] == 'failed' and failed['returncode'] == 3
    warning = verification.run_step(
        'warning', [sys.executable, '-c', 'import sys; print("warning", file=sys.stderr)'],
        tmp_path, cwd=tmp_path, clean_stderr=True,
    )
    assert warning['status'] == 'failed' and warning['returncode'] == 0
    assert warning['stderr_bytes'] > 0


def test_missing_program_is_an_explicit_failure(tmp_path):
    failed = verification.run_step('missing', [str(tmp_path / 'absent')], tmp_path, cwd=tmp_path)
    assert failed['status'] == 'failed' and failed['error'] == 'FileNotFoundError'


def test_clean_command_has_successful_evidence(tmp_path):
    result = verification.run_step(
        'clean', [sys.executable, '-c', 'print("ok")'], tmp_path,
        cwd=tmp_path, clean_stderr=True,
    )
    assert result['status'] == 'passed'
    assert (tmp_path / 'clean.stdout').read_text().strip() == 'ok'
    assert result['stderr_bytes'] == 0


def test_preflight_detects_wrong_interpreter_and_does_not_echo_dsn(monkeypatch):
    monkeypatch.setattr(verification.sys, 'version_info', (3, 13, 5))
    monkeypatch.setenv('MSG_TEST_POSTGRES_URL_TEMPLATE', 'postgresql://user:secret@db.example/{database}')
    report = verification.preflight(ROOT)
    assert any('Python 3.15 is required' in reason for reason in report['blockers'])
    assert any('disposable local test service' in reason for reason in report['blockers'])
    assert 'secret' not in str(report)


def test_source_executable_mode_changes_fingerprint(tmp_path):
    path = tmp_path / 'tool'
    path.write_text('content\n')
    path.chmod(0o600)
    before = verification.content_fingerprint(tmp_path, ['tool'])
    path.chmod(0o700)
    assert verification.content_fingerprint(tmp_path, ['tool'])['sha256'] != before['sha256']
