"""Retention planning protects data and package anchors without opening content."""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'plan_deployment_retention.py'
SPEC = importlib.util.spec_from_file_location('deployment_retention', SCRIPT)
retention = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retention)


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / 'releases'
    root.mkdir()
    current = root / 'msgd-0.2.14-20261002.33-x86_64.pkg.tar.zst'
    rollback = root / 'msgd-0.2.14-20261002.30-x86_64.pkg.tar.zst'
    old = root / 'historical' / 'msgd-0.2.3-20261001.2-x86_64.pkg.tar.zst'
    old.parent.mkdir()
    for path in (current, rollback, old):
        path.write_bytes(b'fixture package, not a real release')
        path.with_name(path.name + '.sig').write_bytes(b'fixture signature')
    return root, current, rollback, old


def planned(archive, **kwargs):
    root, current, rollback, _ = archive
    return retention.plan([root], current, rollback, rollback_tested=True, min_age_days=0, **kwargs)


def test_only_historical_packages_are_candidates_and_no_content_is_opened(archive, monkeypatch):
    root, current, rollback, old = archive
    for name in (
        'service.zip',
        'root-backup.zip',
        'pin-candidate.py',
        'source.tar.gz',
        'notes.txt',
    ):
        (root / name).write_bytes(b'protected fixture')
    (root / 'unpaired-msgd-0.2.3.pkg.tar.zst').write_bytes(b'unrecognized')
    (root / 'msgd-0.2.3-20261001.9-x86_64.pkg.tar.zst.sig').write_bytes(b'orphan signature')
    original_open = os.open

    def directories_only(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY, 'the planner attempted to open file content'
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', directories_only)
    result = planned(archive)
    candidates = {item['path'] for item in result['entries'] if item['candidate']}
    assert candidates == {str(old), str(old.with_name(old.name + '.sig'))}
    assert result['candidate_files'] == 2
    assert result['mode'] == 'dry-run-only' and not result['package_content_verified']
    protected = {item['path']: item for item in result['entries'] if not item['candidate']}
    assert 'current_package' in protected[str(current)]['reasons']
    assert 'verified_rollback_package' in protected[str(rollback)]['reasons']
    assert all(str(root / name) in protected for name in ('service.zip', 'pin-candidate.py'))
    for item in result['entries']:
        assert set(item['identity']) == {
            'device',
            'inode',
            'bytes',
            'mtime_ns',
            'ctime_ns',
            'nlink',
        }


def test_current_and_rollback_duplicates_and_signatures_are_protected(archive):
    root, current, rollback, _ = archive
    copies = root / 'another-deployment'
    copies.mkdir()
    for original in (current, rollback):
        (copies / original.name).write_bytes(b'copy')
        (copies / (original.name + '.sig')).write_bytes(b'copy signature')
    result = planned(archive)
    assert all(
        not item['candidate']
        for item in result['entries']
        if '/another-deployment/' in item['path']
    )


def test_rollback_requires_real_test_attestation_and_distinct_existing_package(archive):
    root, current, rollback, _ = archive
    with pytest.raises(ValueError, match='real rollback test'):
        retention.plan([root], current, rollback)
    with pytest.raises(ValueError, match='distinct'):
        retention.plan([root], current, current, rollback_tested=True)
    with pytest.raises(ValueError, match='missing'):
        retention.plan(
            [root], current, root / 'msgd-0.1-1-x86_64.pkg.tar.zst', rollback_tested=True
        )
    copies = root / 'copy'
    copies.mkdir()
    duplicate = copies / current.name
    duplicate.write_bytes(b'copy')
    with pytest.raises(ValueError, match='distinct package release'):
        retention.plan([root], current, duplicate, rollback_tested=True)


@pytest.mark.parametrize('placement', ['file', 'directory', 'ancestor'])
def test_symlink_paths_fail_the_entire_plan(archive, placement):
    root, current, rollback, _ = archive
    if placement == 'file':
        (root / 'escaped-package.pkg.tar.zst').symlink_to(current)
    elif placement == 'directory':
        (root / 'escaped-directory').symlink_to(root, target_is_directory=True)
    else:
        linked = root / 'linked-root'
        linked.symlink_to(root, target_is_directory=True)
        root, current, rollback = linked, linked / current.name, linked / rollback.name
    with pytest.raises((ValueError, OSError)):
        retention.plan([root], current, rollback, rollback_tested=True)


def test_hardlinks_runtime_directories_and_recent_signature_protect_package_group(archive):
    root, _, _, old = archive
    os.link(old, old.parent / 'msgd-0.2.3-20261001.3-x86_64.pkg.tar.zst')
    for directory in ('root-material', 'service', 'business-backup'):
        private = root / directory
        private.mkdir()
        (private / 'msgd-0.1-1-x86_64.pkg.tar.zst').write_bytes(b'protected fixture')
    result = planned(archive)
    assert not any(item['candidate'] for item in result['entries'])
    assert not any('/root-material/' in item['path'] for item in result['entries'])
    assert not any('/service/' in item['path'] for item in result['entries'])
    assert not any('/business-backup/' in item['path'] for item in result['entries'])
    reasons = {item['path']: item['reasons'] for item in result['entries']}
    assert 'hardlinked_file' in reasons[str(old)]
    assert 'hardlinked_file' in reasons[str(old.with_name(old.name + '.sig'))]

    old.unlink()
    (old.parent / 'msgd-0.2.3-20261001.3-x86_64.pkg.tar.zst').unlink()
    old.write_bytes(b'old fixture')
    future = time.time() + 86400 * 30
    os.utime(old.with_name(old.name + '.sig'), (future + 3600, future + 3600))
    result = planned(archive, now=future)
    assert not any(item['candidate'] for item in result['entries'])


@pytest.mark.parametrize(
    'root', ['/etc/msgd', '/var/lib/msgd-root', '/var/backups', '/var/cache/pacman/pkg']
)
def test_sensitive_shared_and_overbroad_roots_are_rejected_without_scanning(root):
    with pytest.raises(ValueError, match='refused'):
        retention.plan(
            [root], Path(root) / 'current', Path(root) / 'rollback', rollback_tested=True
        )


@pytest.mark.parametrize('name', ['root-material', 'data', 'business-backup', 'secret.keys'])
def test_explicit_sensitive_root_cannot_bypass_subdirectory_protection(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    with pytest.raises(ValueError, match='secret directory root refused'):
        retention.plan([root], root / 'current', root / 'rollback', rollback_tested=True)
    if name == 'root-material':
        with pytest.raises(ValueError, match='secret directory root refused'):
            retention.plan(
                [root / 'releases'],
                root / 'releases/current',
                root / 'releases/rollback',
                rollback_tested=True,
            )


def test_default_age_window_preserves_new_files_and_cli_has_no_execution_mode(archive):
    root, current, rollback, _ = archive
    result = retention.plan([root], current, rollback, rollback_tested=True)
    assert result['candidate_files'] == 0 and result['minimum_age_days'] == 7
    command = [
        sys.executable,
        str(SCRIPT),
        '--root',
        str(root),
        '--current-package',
        str(current),
        '--verified-rollback-package',
        str(rollback),
        '--confirm-rollback-tested',
        '--min-age-days',
        '0',
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)['candidate_files'] == 2
    assert all(path.exists() for path in (current, rollback))
    rejected = subprocess.run([*command, '--apply'], text=True, capture_output=True)
    assert rejected.returncode != 0 and 'unrecognized arguments' in rejected.stderr


def test_private_package_cache_is_shallow_and_does_not_enter_other_projects(archive):
    root, current, rollback, old = archive
    result = retention.plan(
        [], current, rollback, rollback_tested=True, min_age_days=0, package_caches=[root]
    )
    assert result['candidate_files'] == 0
    assert not any(item['path'] == str(old) for item in result['entries'])
    assert result['shallow_package_caches'] == [str(root)]


def test_directory_identity_change_during_open_refuses_stale_inventory(archive, monkeypatch):
    _, _, _, old = archive
    original = os.fstat
    inode = old.parent.stat().st_ino

    def replaced_directory(descriptor):
        value = original(descriptor)
        if value.st_ino == inode:
            return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino + 1)
        return value

    monkeypatch.setattr(os, 'fstat', replaced_directory)
    with pytest.raises(ValueError, match='directory identity changed'):
        planned(archive)


@pytest.mark.parametrize('name', ['msgd_0.2.14-1_amd64.deb', 'msgctl-server-0.2.14-1.x86_64.rpm'])
def test_deb_and_rpm_names_are_recognized_but_similar_unknown_files_are_protected(archive, name):
    root, _, _, _ = archive
    (root / name).write_bytes(b'fixture')
    (root / (name + '.backup')).write_bytes(b'protected fixture')
    result = planned(archive)
    entries = {item['path']: item for item in result['entries']}
    assert entries[str(root / name)]['candidate']
    assert not entries[str(root / (name + '.backup'))]['candidate']
