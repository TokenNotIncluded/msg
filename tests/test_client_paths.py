"""Filesystem layout, safe legacy migration, and profile isolation."""

from pathlib import Path

import pytest

from msg.client import ClientState
from msg.core.errors import Failure
from msg.paths import ClientPaths
from msg.security.crypto import Ed25519Signer


@pytest.fixture
def xdg_home(monkeypatch, tmp_path):
    monkeypatch.setenv('MSG_SERVER', 'https://work.example.org')
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
    assert state.key_path == xdg_home / 'data/msg/services/work.example.org/identity.key'
    assert state.path == xdg_home / 'state/msg/services/work.example.org/client.json'
    assert state.file('oauth-session.json').parent == state.path.parent
    assert state.cache_directory == xdg_home / 'cache/msg/services/work.example.org'
    assert state.temporary_directory == xdg_home / 'runtime/msg'
    assert ClientState(profile='personal', server='https://personal.example.org').signer is None
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


def test_missing_explicit_migration_source_does_not_create_a_profile(xdg_home):
    with pytest.raises(Failure, match='client_migration_source_missing'):
        ClientState(profile='work', migrate_from=xdg_home / 'missing')
    assert not (xdg_home / 'state/msg/services/work.example.org/client.json').exists()


def test_domains_isolate_keys_tokens_certificates_journals_and_cache(xdg_home):
    first = ClientState(server='https://first.example.org')
    first.save_signer(Ed25519Signer.generate())
    first.ensure_encryption_key()
    first.data.update(
        subject_id=first.signer.key_id, certificates=['cert_first'], token={'value': 'first'}
    )
    first._save()
    first.file('token-rotation.json').write_bytes(b'private journal')
    first.file('token-rotation.json').chmod(0o600)
    second = ClientState(server='https://second.example.org')
    assert second.signer is None and second.encryption_recipient is None
    assert second.subject is None and second.certificates == () and second.token is None
    assert not second.file('token-rotation.json').exists()
    assert first.cache_directory != second.cache_directory
    second.save_signer(Ed25519Signer.generate())
    assert second.signer.public_key != first.signer.public_key
    restored = ClientState(server='https://FIRST.example.org:443/')
    assert restored.signer.public_key == first.signer.public_key
    assert restored.data == first.data


def test_profile_aliases_and_equivalent_origins_share_one_identity(xdg_home, monkeypatch):
    monkeypatch.delenv('MSG_SERVER')
    first = ClientState(server='https://EXAMPLE.org:443/', profile='work')
    first.save_signer(Ed25519Signer.generate())
    second = ClientState(server='https://example.org', profile='personal')
    assert second.key_path == first.key_path
    assert second.signer.public_key == first.signer.public_key
    assert ClientState(profile='work').key_path == first.key_path


def test_new_installation_has_no_hardcoded_public_service(xdg_home, monkeypatch):
    monkeypatch.delenv('MSG_SERVER')
    with pytest.raises(Failure, match='server_required'):
        ClientState()
    assert not (xdg_home / 'data/msg').exists()
    ClientState(server='https://own.example.org')
    assert ClientState().server == 'https://own.example.org'


def test_wrong_domain_migration_leaves_source_and_destination_untouched(xdg_home):
    old = ClientState(xdg_home / 'legacy', server='https://first.example.org')
    old.save_signer(Ed25519Signer.generate())
    before = {path.name: path.read_bytes() for path in old.directory.iterdir() if path.is_file()}
    with pytest.raises(Failure, match='client_server_mismatch'):
        ClientState(server='https://second.example.org', migrate_from=old.directory)
    assert {
        path.name: path.read_bytes() for path in old.directory.iterdir() if path.is_file()
    } == before
    assert not (xdg_home / 'data/msg/services/second.example.org').exists()


def test_old_split_profile_migrates_keys_and_state_together(xdg_home):
    previous = ClientPaths.discover(profile='old')
    old = ClientState(paths=previous, server='https://work.example.org')
    old.save_signer(Ed25519Signer.generate())
    old.ensure_encryption_key()
    old.file('pending.json').write_bytes(b'{}')
    old.file('pending.json').chmod(0o600)
    new = ClientState(profile='old')
    assert new.signer.public_key == old.signer.public_key
    assert new.encryption_recipient == old.encryption_recipient
    assert new.file('pending.json').read_bytes() == b'{}'
    assert not old.key_path.exists() and not old.path.exists()
    assert ClientState(profile='old').signer.public_key == new.signer.public_key


@pytest.mark.parametrize(
    'server',
    [
        'https://user:password@example.org',
        'https://example.org/a',
        'https://../',
        'https://example.org:99999',
        'https://example.org?token=secret',
        'https://example.org#fragment',
        'https://example.org\\escape',
        'https://bad_host',
    ],
)
def test_invalid_domain_never_creates_credential_directories(xdg_home, server):
    with pytest.raises(Failure, match='invalid_server_url'):
        ClientState(server=server)
    assert not (xdg_home / 'data/msg').exists()


def test_scheme_port_and_ipv6_namespaces_are_distinct(xdg_home):
    states = [
        ClientState(server=server)
        for server in [
            'https://example.org',
            'http://example.org',
            'https://example.org:8443',
            'http://[::1]:8042',
            'https://[::1]',
        ]
    ]
    assert len({state.directory for state in states}) == len(states)
    assert states[3].directory.name == 'http~ipv6~00000000000000000000000000000001~8042'
