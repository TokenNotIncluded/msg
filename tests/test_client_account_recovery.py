"""Restoring account files must not silently choose or replace an identity."""

from types import SimpleNamespace

import pytest

from msg.atomic_file import durable_write
from msg.client import ClientState
from msg.client_accounts import run_command
from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.paths import ClientPaths
from msg.security.crypto import Ed25519Signer

SERVER = 'https://example.org'


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MSG_SERVER', SERVER)
    for key in ('XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def args(action='list', **extra):
    return SimpleNamespace(
        action=action, config_dir=None, server=SERVER, profile=None, account=None, **extra
    )


def registered(account):
    state = ClientState(account=account)
    state.save_signer(Ed25519Signer.generate())
    state.data.update(handle=account, subject_id=state.signer.key_id)
    state._save()
    return state


def snapshot(root):
    return {
        str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes())
        for path in root.rglob('*')
        if path.is_file()
    }


@pytest.mark.parametrize('accounts', [('alice',), ('default', 'bob')])
def test_missing_selection_requires_explicit_choice_without_writes(home, accounts):
    states = [registered(account) for account in accounts]
    before = snapshot(home)
    with pytest.raises(Failure, match='local_account_selection_required'):
        ClientState()
    assert snapshot(home) == before
    listing = run_command(args())
    assert {item['account'] for item in listing['accounts']} == set(accounts)
    assert not any(item['selected'] for item in listing['accounts'])
    assert snapshot(home) == before
    run_command(args('use', name=accounts[-1]))
    assert ClientState().subject == states[-1].subject
    assert ClientState().signer.public_key == states[-1].signer.public_key


@pytest.mark.parametrize('old_handle', ['alice', 'bob'])
def test_missing_selection_with_legacy_and_restored_identity_requires_choice(home, old_handle):
    restored = registered('alice')
    legacy = ClientState(paths=ClientPaths.discover(server=SERVER), server=SERVER)
    legacy.save_signer(Ed25519Signer.generate())
    legacy.data.update(handle=old_handle, subject_id=legacy.signer.key_id)
    legacy._save()
    assert legacy.subject != restored.subject
    before = snapshot(home)
    with pytest.raises(Failure, match='local_account_selection_required'):
        ClientState()
    assert snapshot(home) == before
    run_command(args('use', name='alice'))
    assert ClientState().subject == restored.subject
    assert ClientState().signer.public_key == restored.signer.public_key
    assert legacy.key_path.exists()


def test_matching_legacy_copy_can_complete_selection_with_original_identity(home):
    restored = registered('alice')
    legacy = ClientState(paths=ClientPaths.discover(server=SERVER), server=SERVER)
    legacy.save_signer(restored.signer)
    legacy.data.update(restored.data)
    legacy._save()
    assert ClientState().subject == restored.subject
    assert ClientState().signer.public_key == restored.signer.public_key


@pytest.mark.parametrize(
    'configuration',
    ['identity.key', 'hardware-signer.json', 'oauth-session.json', 'token', 'api_key'],
)
def test_legacy_identity_does_not_hide_a_partially_restored_account(home, configuration):
    restored = ClientState(account='restored')
    if configuration in {'token', 'api_key'}:
        restored.data[configuration] = {
            'credential_id': 'restored-credential',
            'value': 'test-value',
        }
        restored._save()
    else:
        durable_write(restored.file(configuration), b'partial-restored-material', mode=0o600)
    legacy = ClientState(paths=ClientPaths.discover(server=SERVER), server=SERVER)
    legacy.save_signer(Ed25519Signer.generate())
    legacy.data.update(handle='alice', subject_id=legacy.signer.key_id)
    legacy._save()
    before = snapshot(home)
    with pytest.raises(Failure, match='local_account_selection_required'):
        ClientState()
    assert snapshot(home) == before


def test_account_list_on_fresh_service_creates_no_files(home):
    assert run_command(args()) == {'server': SERVER, 'accounts': []}
    assert list(home.iterdir()) == []


def test_account_list_does_not_migrate_legacy_identity(home):
    paths = ClientPaths.discover(server=SERVER)
    old = ClientState(paths=paths, server=SERVER)
    old.save_signer(Ed25519Signer.generate())
    old.data.update(handle='alice', subject_id=old.signer.key_id)
    old._save()
    before = snapshot(home)
    listing = run_command(args())
    assert listing['accounts'] == []
    assert snapshot(home) == before
    assert ClientState().signer.public_key == old.signer.public_key


def test_stale_selection_never_imports_another_legacy_identity(home):
    bob = registered('bob')
    run_command(args('use', name='bob'))
    bob.path.unlink()
    old = ClientState(ClientPaths.discover().config)
    old.save_signer(Ed25519Signer.generate())
    old.data.update(handle='alice', subject_id=old.signer.key_id)
    old._save()
    before = snapshot(home)
    with pytest.raises(Failure, match='local_account_not_found'):
        ClientState()
    assert snapshot(home) == before
    assert old.key_path.exists()


def test_empty_selected_account_does_not_import_different_legacy_handle(home):
    bob = ClientState(account='bob')
    marker = ClientPaths.discover(server=SERVER).config / 'current-account.json'
    durable_write(marker, canonical({'version': 1, 'account': 'bob'}), mode=0o600)
    old = ClientState(ClientPaths.discover().config)
    old.save_signer(Ed25519Signer.generate())
    old.data.update(handle='alice', subject_id=old.signer.key_id)
    old._save()
    before = snapshot(home)
    selected = ClientState()
    assert selected.account == 'bob' and selected.signer is None
    assert selected.path == bob.path
    assert snapshot(home) == before


def test_rejected_use_keeps_previous_service_and_selection(home, monkeypatch):
    alice = registered('alice')
    run_command(args('use', name='alice'))
    other = 'https://other.example.org'
    ClientState(server=other, account='empty')
    ClientState(server=SERVER)
    monkeypatch.delenv('MSG_SERVER')
    before = snapshot(home)
    request = args('use', name='empty')
    request.server = other
    with pytest.raises(Failure, match='local_account_not_authenticated'):
        run_command(request)
    assert snapshot(home) == before
    assert ClientState().subject == alice.subject


def test_invocation_override_does_not_mark_default_as_selected(home):
    registered('alice')
    registered('bob')
    run_command(args('use', name='alice'))
    request = args()
    request.account = 'bob'
    before = snapshot(home)
    listing = run_command(request)['accounts']
    assert [item['account'] for item in listing if item['selected']] == ['bob']
    assert snapshot(home) == before
    assert ClientState().account == 'alice'


def test_restored_key_without_state_still_requires_account_selection(home):
    state = registered('alice')
    state.path.unlink()
    before = snapshot(home)
    with pytest.raises(Failure, match='local_account_selection_required'):
        ClientState()
    assert snapshot(home) == before
    assert run_command(args())['accounts'] == [
        {
            'account': 'alice',
            'handle': None,
            'subject_id': None,
            'selected': False,
            'signer': 'software',
        }
    ]
    assert snapshot(home) == before


def test_listing_another_service_does_not_change_remembered_service(home, monkeypatch):
    alice = registered('alice')
    run_command(args('use', name='alice'))
    monkeypatch.delenv('MSG_SERVER')
    before = snapshot(home)
    request = args()
    request.server = 'https://other.example.org'
    assert run_command(request) == {'server': request.server, 'accounts': []}
    assert snapshot(home) == before
    assert ClientState().subject == alice.subject


@pytest.mark.parametrize('base', ['config', 'data', 'state'])
def test_listing_rejects_symlinked_account_roots_without_changing_target(home, base):
    registered('alice')
    service = ClientPaths.discover(server=SERVER)
    directory = getattr(service, base) / 'accounts'
    target = directory.with_name('saved-accounts')
    directory.rename(target)
    directory.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    with pytest.raises(Failure, match='unsafe_client_directory'):
        run_command(args())
    assert snapshot(target) == before


def test_explicit_import_restores_missing_selected_state_with_same_key(home):
    alice = registered('alice')
    run_command(args('use', name='alice'))
    backup = ClientState(home / 'backup')
    backup.save_signer(alice.signer)
    backup.data.update(alice.data)
    backup._save()
    alice.path.unlink()
    run_command(args('import', name='alice', directory=backup.directory))
    restored = ClientState()
    assert restored.subject == alice.subject
    assert restored.signer.public_key == alice.signer.public_key


@pytest.mark.parametrize('version', [None, 2])
def test_listing_rejects_unknown_selection_version_without_changes(home, version):
    registered('alice')
    marker = ClientPaths.discover(server=SERVER).config / 'current-account.json'
    durable_write(marker, canonical({'version': version, 'account': 'alice'}), mode=0o600)
    before = snapshot(home)
    with pytest.raises(Failure, match='unknown_client_state_version'):
        run_command(args())
    assert snapshot(home) == before
