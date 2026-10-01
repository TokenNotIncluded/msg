"""Account separation and migration keep credentials and collaboration state intact."""

from types import SimpleNamespace

import pytest

from msg.atomic_file import durable_write
from msg.client import ClientState
from msg.client_accounts import run_command
from msg.client_subagents import LocalAgents
from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.paths import ClientPaths
from msg.security.crypto import Ed25519Signer


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MSG_SERVER', 'https://example.org')
    for key in ('XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def args(action, **extra):
    return SimpleNamespace(
        action=action,
        config_dir=None,
        server='https://example.org',
        profile=None,
        account=None,
        **extra,
    )


def registered(account, handle=None):
    state = ClientState(account=account)
    state.save_signer(Ed25519Signer.generate())
    state.data.update(handle=handle or account, subject_id=state.signer.key_id)
    state._save()
    return state


def test_account_selection_isolates_identity_tokens_and_cache(home):
    alice = registered('alice')
    alice.data['token'] = {'credential_id': 'one', 'value': 'YWJj'}
    alice._save()
    bob = registered('bob')
    assert alice.paths.data != bob.paths.data
    assert alice.cache_directory != bob.cache_directory
    assert bob.token is None
    run_command(args('use', name='bob'))
    assert ClientState().signer.public_key == bob.signer.public_key
    assert ClientState(account='alice').signer.public_key == alice.signer.public_key
    assert ClientState().account == 'bob'  # invocation override does not switch default
    listing = run_command(args('list'))['accounts']
    assert next(x for x in listing if x['account'] == 'bob')['selected']
    assert 'token' not in str(listing) and 'YWJj' not in str(listing)


def test_service_singleton_migrates_under_existing_handle(home):
    old_paths = ClientPaths.discover(server='https://example.org')
    old = ClientState(paths=old_paths, server='https://example.org')
    old.save_signer(Ed25519Signer.generate())
    old.data.update(handle='alice', subject_id=old.signer.key_id)
    old._save()
    new = ClientState()
    assert new.account == 'alice'
    assert new.key_path.parent.name == 'alice'
    assert new.signer.public_key == old.signer.public_key
    assert not old.key_path.exists()
    # Persist the account choice after migrating the singleton.
    assert ClientState().signer.public_key == new.signer.public_key


def test_portable_import_preserves_subagent_database_and_listener_cursor(home):
    old = ClientState(home / 'portable')
    old.data.update(handle='alice', subject_id='u_alice')
    old._save()
    LocalAgents(old).create('worker')
    cursor = old.paths.state / 'agent-listeners' / 'cursor.json'
    cursor.parent.mkdir(mode=0o700)
    durable_write(cursor, canonical({'cursor': 'previous'}), mode=0o600)
    run_command(args('import', name='alice', directory=old.directory))
    new = ClientState(account='alice')
    assert LocalAgents(new).list()[0]['name'] == '@alice#worker'
    assert (new.paths.state / 'agent-listeners/cursor.json').read_bytes() == canonical({
        'cursor': 'previous'
    })
    assert not cursor.exists()
    assert not old.path.exists()


def test_import_conflict_keeps_source_and_destination(home):
    current = registered('alice')
    old = ClientState(home / 'portable')
    old.save_signer(Ed25519Signer.generate())
    before = old.key_path.read_bytes()
    with pytest.raises(Failure, match='client_migration_conflict'):
        run_command(args('import', name='alice', directory=old.directory))
    assert old.key_path.read_bytes() == before
    assert ClientState(account='alice').signer.public_key == current.signer.public_key


@pytest.mark.parametrize('name', ['../escape', '.', '/tmp/x', 'x/y', '', 'x' * 65])
def test_invalid_account_never_creates_state(home, name):
    with pytest.raises(Failure, match='invalid_account_name'):
        ClientState(account=name)
    assert not (home / '.local/state/msg').exists()


def test_use_missing_or_unregistered_account_keeps_selection(home):
    registered('alice')
    run_command(args('use', name='alice'))
    with pytest.raises(Failure, match='local_account_not_found'):
        run_command(args('use', name='missing'))
    assert ClientState().account == 'alice'
    assert not ClientPaths.discover(server='https://example.org', account='missing').state.exists()


def test_import_old_split_service_keeps_keys_and_nested_data(home):
    paths = ClientPaths.discover(server='https://example.org')
    old = ClientState(paths=paths, server='https://example.org')
    old.save_signer(Ed25519Signer.generate())
    old.data.update(subject_id=old.signer.key_id, handle='alice')
    old._save()
    LocalAgents(old).create('worker')
    run_command(args('import', name='alice', directory=old.directory))
    restored = ClientState(account='alice')
    assert restored.signer.public_key == old.signer.public_key
    assert LocalAgents(restored).list()[0]['name'] == '@alice#worker'
    assert not old.key_path.exists()


def test_other_account_does_not_steal_old_singleton(home):
    paths = ClientPaths.discover(server='https://example.org')
    old = ClientState(paths=paths, server='https://example.org')
    old.save_signer(Ed25519Signer.generate())
    old.data.update(handle='alice', subject_id=old.signer.key_id)
    old._save()
    bob = ClientState(account='bob')
    assert bob.signer is None
    assert old.key_path.exists()
    assert ClientState().signer.public_key == old.signer.public_key


def test_migration_rejects_nested_symlink_without_removing_source(home):
    old = ClientState(home / 'portable')
    old.save_signer(Ed25519Signer.generate())
    folder = old.paths.state / 'agent-listeners'
    folder.mkdir(mode=0o700)
    (folder / 'cursor.json').symlink_to(home / 'outside')
    with pytest.raises(Failure, match='unsafe_legacy_client_file'):
        run_command(args('import', name='alice', directory=old.directory))
    assert old.key_path.exists() and old.path.exists()
