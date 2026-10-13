"""Local evidence belongs to actual uncommitted files, not an old commit."""

import importlib.util
import subprocess
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
        'exit',
        [sys.executable, '-c', 'raise SystemExit(3)'],
        tmp_path,
        cwd=tmp_path,
    )
    assert failed['status'] == 'failed' and failed['returncode'] == 3
    warning = verification.run_step(
        'warning',
        [sys.executable, '-c', 'import sys; print("warning", file=sys.stderr)'],
        tmp_path,
        cwd=tmp_path,
        clean_stderr=True,
    )
    assert warning['status'] == 'failed' and warning['returncode'] == 0
    assert warning['stderr_bytes'] > 0


def test_missing_program_is_an_explicit_failure(tmp_path):
    failed = verification.run_step('missing', [str(tmp_path / 'absent')], tmp_path, cwd=tmp_path)
    assert failed['status'] == 'failed' and failed['error'] == 'FileNotFoundError'


def test_clean_command_has_successful_evidence(tmp_path):
    result = verification.run_step(
        'clean',
        [sys.executable, '-c', 'print("ok")'],
        tmp_path,
        cwd=tmp_path,
        clean_stderr=True,
    )
    assert result['status'] == 'passed'
    assert (tmp_path / 'clean.stdout').read_text().strip() == 'ok'
    assert result['stderr_bytes'] == 0


def test_preflight_detects_wrong_interpreter_and_does_not_echo_dsn(monkeypatch):
    monkeypatch.setattr(verification.sys, 'version_info', (3, 13, 5))
    monkeypatch.setenv(
        'MSG_TEST_POSTGRES_URL_TEMPLATE', 'postgresql://user:secret@db.example/{database}'
    )
    report = verification.preflight(ROOT)
    assert any('Python 3.15 is required' in reason for reason in report['blockers'])
    assert any('disposable local test service' in reason for reason in report['blockers'])
    assert 'secret' not in str(report)


@pytest.fixture
def gitlink_checkout(tmp_path):
    def git(directory, *args):
        return (
            subprocess
            .check_output(
                [
                    'git',
                    '-C',
                    str(directory),
                    '-c',
                    'user.name=Fixture',
                    '-c',
                    'user.email=fixture@example.org',
                    '-c',
                    'commit.gpgsign=false',
                    *args,
                ],
                stderr=subprocess.PIPE,
            )
            .decode()
            .strip()
        )

    git(tmp_path, 'init')
    child = tmp_path / 'module'
    child.mkdir()
    git(child, 'init')
    (child / 'source.py').write_text('value = 1\n')
    git(child, 'add', 'source.py')
    git(child, 'commit', '-m', 'fixture')
    revision = git(child, 'rev-parse', 'HEAD')
    git(tmp_path, 'update-index', '--add', '--cacheinfo', f'160000,{revision},module')
    git(tmp_path, 'commit', '-m', 'pin module')
    return tmp_path, child, git


def test_source_snapshot_records_clean_gitlink(gitlink_checkout):
    root, _, _ = gitlink_checkout
    snapshot = verification.source_snapshot(root)
    assert snapshot['dirty'] is False
    assert snapshot['files'][0]['kind'] == 'gitlink'
    assert snapshot['files'][0]['path'] == 'module'
    assert snapshot == verification.source_snapshot(root)


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'revision', 'missing', 'symlink'])
def test_source_snapshot_refuses_unverified_gitlink(gitlink_checkout, change):
    root, child, git = gitlink_checkout
    if change == 'tracked':
        (child / 'source.py').write_text('changed\n')
    elif change == 'untracked':
        (child / 'new.py').write_text('new source\n')
    elif change == 'revision':
        (child / 'source.py').write_text('changed\n')
        git(child, 'add', 'source.py')
        git(child, 'commit', '-m', 'unpinned revision')
    else:
        child.rename(root / 'moved')
        if change == 'symlink':
            child.symlink_to(root / 'moved', target_is_directory=True)
    with pytest.raises(ValueError, match='submodule'):
        verification.source_snapshot(root)


def test_source_executable_mode_changes_fingerprint(tmp_path):
    path = tmp_path / 'tool'
    path.write_text('content\n')
    path.chmod(0o600)
    before = verification.content_fingerprint(tmp_path, ['tool'])
    path.chmod(0o700)
    assert verification.content_fingerprint(tmp_path, ['tool'])['sha256'] != before['sha256']
