"""Filesystem layout, safe legacy migration, and profile isolation."""

from pathlib import Path

import pytest

from msg.client import ClientState
from msg.core.errors import Failure
from msg.paths import ClientPaths
from msg.security.crypto import Ed25519Signer


@pytest.fixture
def xdg_home(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
    for variable, name in [
        ('XDG_CONFIG_HOME', 'config'),
        ('XDG_DATA_HOME', 'data'),
        ('XDG_STATE_HOME', 'state'),
        ('XDG_CACHE_HOME', 'cache'),
    ]:
        monkeypatch.setenv(variable, str(tmp_path / name))
    return tmp_path


def test_default_paths_separate_credentials_state_and_cache(xdg_home):
    state = ClientState(profile='work')
    state.save_signer(Ed25519Signer.generate())
    state.ensure_encryption_key()
    assert state.key_path == xdg_home / 'data/msg/profiles/work/identity.key'
    assert state.path == xdg_home / 'state/msg/profiles/work/client.json'
    assert state.file('oauth-session.json').parent == state.path.parent
    assert state.cache_directory == xdg_home / 'cache/msg/profiles/work'
    assert state.temporary_directory == xdg_home / 'runtime/msg'
    assert ClientState(profile='personal').signer is None
    assert ClientState(paths=state.paths).signer.private_bytes() == state.signer.private_bytes()
    for path in (state.path, state.key_path, state.age_key_path):
        assert path.stat().st_mode & 0o077 == 0


def test_relative_xdg_variables_are_ignored(xdg_home, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', 'relative')
    assert ClientPaths.discover().data == Path.home() / '.local/share/msg'


def test_legacy_default_migrates_without_replacing_identity(xdg_home):
    legacy = ClientPaths.discover().config
    old = ClientState(legacy)
    old.save_signer(Ed25519Signer.generate())
    old.ensure_encryption_key()
    original = old.signer.private_bytes()
    old.file('token-rotation.json').write_bytes(b'{}')
    old.file('token-rotation.json').chmod(0o600)
    new = ClientState()
    assert new.signer.private_bytes() == original
    assert new.encryption_recipient == old.encryption_recipient
    assert new.file('token-rotation.json').exists()
    assert not old.key_path.exists() and not old.path.exists()
    assert ClientState().signer.private_bytes() == original


def test_migration_conflict_retains_every_source_file(xdg_home):
    old = ClientState(xdg_home / 'legacy')
    old.save_signer(Ed25519Signer.generate())
    new = ClientState(profile='work')
    new.save_signer(Ed25519Signer.generate())
    with pytest.raises(Failure, match='client_migration_conflict'):
        ClientState(profile='work', migrate_from=old.directory)
    assert old.key_path.exists() and old.path.exists()


def test_symlink_directory_is_rejected_without_chmod_target(xdg_home):
    target = xdg_home / 'target'
    target.mkdir(mode=0o755)
    link = xdg_home / 'data/msg'
    link.parent.mkdir()
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(Failure, match='unsafe_client_directory'):
        ClientState()
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize('profile', ['../escape', '/absolute', 'a/b', '', 'x' * 65])
def test_profile_name_cannot_escape(profile):
    with pytest.raises(Failure, match='invalid_profile_name'):
        ClientPaths.discover(profile=profile)
