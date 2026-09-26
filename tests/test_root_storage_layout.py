"""New installs isolate root CA private state from service configuration."""

import stat
from pathlib import Path

from msg.admin.root import root_envelope
from msg.config import load_settings, root_private_dir


def test_new_installation_keeps_root_private_material_outside_etc(installation_seed):
    directory, _, _, _ = installation_seed
    settings = load_settings(directory / 'etc')
    assert settings.root_private_dir == directory / 'etc-root'
    key = settings.root_private_dir / 'key.json'
    assert key.is_file()
    assert stat.S_IMODE(settings.root_private_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not (settings.config_dir / 'root' / 'key.json').exists()
    assert settings.trust_file.is_file()
    assert settings.service_keys == directory / 'data' / 'service'
    assert (settings.service_keys / 'online.key').is_file()
    assert not (settings.config_dir / 'service' / 'online.key').exists()


def test_default_and_legacy_root_locations_are_unambiguous(tmp_path):
    assert root_private_dir(Path('/etc/msgd')) == Path('/var/lib/msgd-root')
    config = tmp_path / 'etc'
    legacy = config / 'root' / 'key.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('legacy envelope')
    assert root_envelope(config) == legacy
    current = root_private_dir(config) / 'key.json'
    current.parent.mkdir(parents=True)
    current.write_text('new envelope')
    assert root_envelope(config) == current
