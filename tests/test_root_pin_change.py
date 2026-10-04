import pytest

from msg.admin.root import RootAdmin
from msg.core.codec import canonical, loads
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer, open_private_key, seal_private_key


def test_short_pin_is_explicit_and_same_root_key_is_preserved(tmp_path, monkeypatch):
    root = Ed25519Signer.generate()
    old = 'isolated-old-root-passphrase'
    short = '123456'
    path = tmp_path / 'root.json'
    path.write_bytes(canonical(seal_private_key(root.private_bytes(), old)))
    with pytest.raises(Failure, match='weak_pin'):
        seal_private_key(root.private_bytes(), short)
    monkeypatch.setattr(RootAdmin, '_provisioning_operator', lambda self: 'private-terminal')
    monkeypatch.setattr('msg.admin.root.root_envelope', lambda config: path)
    answers = iter([old, short, short])
    monkeypatch.setattr('getpass.getpass', lambda prompt: next(answers))
    monkeypatch.setattr(
        'builtins.input', lambda prompt: pytest.fail('No typed confirmation needed')
    )
    RootAdmin(tmp_path, allow_ssh=True).change_pin(allow_short_pin=True)
    assert open_private_key(loads(path.read_bytes()), short) == root.private_bytes()
    assert path.stat().st_mode & 0o777 == 0o600


def test_wrong_current_pin_leaves_envelope_unchanged(tmp_path, monkeypatch):
    root = Ed25519Signer.generate()
    path = tmp_path / 'root.json'
    before = canonical(seal_private_key(root.private_bytes(), 'isolated-old-root-passphrase'))
    path.write_bytes(before)
    monkeypatch.setattr(RootAdmin, '_provisioning_operator', lambda self: 'private-terminal')
    monkeypatch.setattr('msg.admin.root.root_envelope', lambda config: path)
    monkeypatch.setattr('getpass.getpass', lambda prompt: 'wrong-current-passphrase')
    with pytest.raises(Failure, match='invalid_pin'):
        RootAdmin(tmp_path).change_pin(allow_short_pin=True)
    assert path.read_bytes() == before
