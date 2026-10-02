"""Service users must read native payloads built with a private umask."""

import importlib.util
import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('build_native', ROOT / 'packaging/build-native.py')
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


def test_restrictive_umask_payload_is_readable_and_executable(tmp_path):
    payload = tmp_path / 'usr'
    pth = payload / 'lib/msgd/python3.15/lib/python3.15/site-packages/msgd.pth'
    module = payload / 'lib/msgd/site-packages/msg/__init__.py'
    interpreter = payload / 'lib/msgd/python3.15/bin/python3.15'
    launcher = payload / 'bin/msgd'
    previous = os.umask(0o077)
    try:
        for path, content in (
            (pth, '../../../../site-packages\n'),
            (module, 'VERSION = "0.2.14"\n'),
            (interpreter, '#!/bin/sh\nexit 0\n'),
            (launcher, '#!/bin/sh\nexit 0\n'),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        interpreter.chmod(0o700)
        launcher.chmod(0o700)
        assert stat.S_IMODE(pth.stat().st_mode) == 0o600
        assert stat.S_IMODE(module.parent.stat().st_mode) == 0o700
        native.normalize_payload_modes(payload)
    finally:
        os.umask(previous)

    for directory in (payload, *payload.rglob('*')):
        if directory.is_dir():
            assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    for path in (pth, module):
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    for path in (interpreter, launcher):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert pth.read_text() == '../../../../site-packages\n'


def test_payload_mode_normalization_does_not_follow_symlinks(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    secret = private / 'secret'
    secret.write_text('outside the package\n')
    secret.chmod(0o600)
    payload = tmp_path / 'usr'
    payload.mkdir(mode=0o700)
    directory_link = payload / 'external-directory'
    directory_link.symlink_to(private, target_is_directory=True)
    file_link = payload / 'external-file'
    file_link.symlink_to(secret)

    native.normalize_payload_modes(payload)

    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert directory_link.is_symlink() and directory_link.readlink() == private
    assert file_link.is_symlink() and file_link.readlink() == secret
