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
