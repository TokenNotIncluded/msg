from pathlib import Path

import pytest

from msg.config import root_private_dir
from msg.core.errors import Failure
from msg.daemon import main, parser
from msg.paths import ServerPaths


def test_independent_instances_do_not_derive_paths_from_domains():
    a, b = ServerPaths.for_instance('main'), ServerPaths.for_instance('sandbox')
    assert a.config == Path('/etc/msgd/main')
    assert a.data == Path('/var/lib/msgd/main')
    assert a.root == Path('/var/lib/private/msgd/main/root')
    assert a.cache == Path('/var/cache/msgd/main')
    assert a.runtime == Path('/run/msgd/main')
    assert a != b
    assert root_private_dir(a.config) == a.root
    assert not a.root.is_relative_to(a.data)
    assert root_private_dir(Path('/etc/msgd')) == Path('/var/lib/msgd-root')
    assert root_private_dir(Path('/tmp/test-etc')) == Path('/tmp/test-etc-root')


@pytest.mark.parametrize('name', ['', '../main', 'a/b', 'msg.example.org', 'a' * 27, '-a', 'A'])
def test_instance_names_cannot_escape_shared_parents(name):
    with pytest.raises(Failure, match='invalid_instance_name'):
        ServerPaths.for_instance(name)


def test_instance_and_explicit_directory_are_exclusive():
    with pytest.raises(SystemExit):
        parser().parse_args(['--instance', 'main', '--config-dir', '/tmp/other', 'serve'])


def test_new_instance_requires_explicit_origin_and_database_before_writing(capsys):
    assert main(['--instance', 'main', 'init']) == 2
    assert 'instance_service_url_required' in capsys.readouterr().out
    assert main(['--instance', 'main', 'init', '--service-url', 'https://example.org']) == 2
    assert 'instance_postgres_dsn_required' in capsys.readouterr().out


def test_named_service_templates_isolate_accounts_and_root():
    base = Path(__file__).parents[1] / 'deploy'
    for name in ('msgd@.service', 'msgd-worker@.service'):
        unit = (base / name).read_text()
        assert 'User=msgd-%i' in unit
        assert '--instance %i' in unit
        assert 'InaccessiblePaths=/var/lib/private/msgd' in unit
        assert 'KillMode=mixed' in unit
        assert 'ReadWritePaths=/var/lib/msgd/%i' in unit
        assert 'msg.lmm.best' not in unit


def test_upgrade_preserves_existing_explicit_subdirectory_root(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import msg.config as config

    monkeypatch.setattr(config, 'SERVER_CONFIG_DIR', tmp_path / 'etc')
    current = tmp_path / 'private/main/root'
    monkeypatch.setattr(
        config.ServerPaths, 'for_instance', lambda name: SimpleNamespace(root=current)
    )
    legacy = tmp_path / 'etc/main-root'
    legacy.mkdir(parents=True)
    (legacy / 'key.json').write_text('existing sealed envelope')
    assert config.root_private_dir(tmp_path / 'etc/main') == legacy
    current.mkdir(parents=True)
    (current / 'key.json').write_text('different sealed envelope')
    with pytest.raises(Failure, match='ambiguous_root_private_directory'):
        config.root_private_dir(tmp_path / 'etc/main')
