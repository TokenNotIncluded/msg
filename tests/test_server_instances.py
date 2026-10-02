import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from msg.config import root_private_dir
from msg.core.errors import Failure
from msg.daemon import main, parser
from msg.paths import ServerPaths


@pytest.mark.parametrize('readable,searchable', [(True, True), (False, True), (True, False)])
def test_network_runtime_rejects_access_to_actual_root_without_reading_it(
    monkeypatch, tmp_path, readable, searchable
):
    from msg import daemon

    protected = tmp_path / 'private/root'
    settings = SimpleNamespace(config_dir=tmp_path / 'etc', root_private_dir=protected)
    probes = []

    def access(path, mode):
        probes.append((path, mode))
        return (
            path == protected
            and (not mode & os.R_OK or readable)
            and (not mode & os.X_OK or searchable)
        )

    monkeypatch.setattr(daemon.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(daemon.os, 'access', access)
    with pytest.raises(Failure, match='root_material_accessible_to_service'):
        daemon.network_runtime(settings)
    assert any(path == protected for path, _ in probes)


@pytest.fixture
def named_layout(monkeypatch, tmp_path):
    from msg import config, daemon

    layout = ServerPaths(
        tmp_path / 'etc/main',
        tmp_path / 'data/main',
        tmp_path / 'private/main/root',
        tmp_path / 'cache/main',
        tmp_path / 'runtime/main',
    )
    monkeypatch.setattr(config, 'SERVER_CONFIG_DIR', tmp_path / 'etc')
    monkeypatch.setattr(daemon.ServerPaths, 'for_instance', lambda name: layout)
    return layout


@pytest.mark.parametrize('command', ['serve', 'worker', 'hosting'])
def test_named_runtime_rejects_storage_fallback_before_application_or_listener(
    monkeypatch, named_layout, capsys, command
):
    import uvicorn

    from msg import daemon
    from msg.config import write_example

    write_example(
        named_layout.config,
        named_layout.data,
        'https://example.org',
        postgres_dsn='service=main',
    )
    config = named_layout.config / 'msgd.toml'
    config.write_text(
        '\n'.join(
            line for line in config.read_text().splitlines() if not line.startswith('content = ')
        )
    )
    monkeypatch.setattr(daemon, 'network_runtime', lambda settings: None)
    monkeypatch.setattr(uvicorn, 'run', lambda *args, **kwargs: pytest.fail('started a listener'))
    monkeypatch.setattr(
        daemon, 'worker_loop', lambda *args, **kwargs: pytest.fail('started worker')
    )
    assert daemon.main(['--instance', 'main', command]) == 1
    assert 'instance_storage_layout_mismatch' in capsys.readouterr().err
    assert not named_layout.data.exists()


@pytest.mark.parametrize(
    'artifact',
    [
        'key.json',
        'initialization.pending',
        'root/key.json',
        'root/initialization.pending',
        'rotation.pending.json',
        'history',
        '.rotation.lock',
        'root/rotation.pending.json',
        'root/history',
    ],
)
def test_named_init_rejects_legacy_root_collision_before_configuration_writes(
    monkeypatch, named_layout, capsys, artifact
):
    from msg import daemon

    named_layout.config.mkdir(parents=True)
    target = named_layout.config / artifact
    target.parent.mkdir(exist_ok=True)
    if target.name == 'history':
        target.mkdir(mode=0o700)
    else:
        target.touch(mode=0o600)
    assert (
        daemon.main([
            '--instance',
            'main',
            'init',
            '--service-url',
            'https://example.org',
            '--postgres-dsn',
            'service=main',
        ])
        == 2
    )
    assert 'instance_config_contains_root_material' in capsys.readouterr().out
    assert not (named_layout.config / 'msgd.toml').exists()
    assert not named_layout.root.exists()


def test_named_init_rejects_custom_data_before_creating_directories(named_layout, capsys):
    from msg import daemon

    assert (
        daemon.main([
            '--instance',
            'main',
            'init',
            '--service-url',
            'https://example.org',
            '--postgres-dsn',
            'service=main',
            '--data-dir',
            str(named_layout.data.parent / 'other'),
        ])
        == 2
    )
    assert 'instance_storage_layout_mismatch' in capsys.readouterr().out
    assert not named_layout.config.exists()


def test_named_init_rejects_dangling_root_artifact_symlink(named_layout, capsys):
    from msg import daemon

    named_layout.config.mkdir(parents=True)
    (named_layout.config / 'key.json').symlink_to(named_layout.config / 'missing')
    assert (
        daemon.main([
            '--instance',
            'main',
            'init',
            '--service-url',
            'https://example.org',
            '--postgres-dsn',
            'service=main',
        ])
        == 2
    )
    assert 'instance_config_contains_root_material' in capsys.readouterr().out
    assert not (named_layout.config / 'msgd.toml').exists()


@pytest.mark.parametrize('command', ['serve', 'worker'])
def test_valid_named_runtime_still_starts(monkeypatch, named_layout, command):
    import uvicorn

    from msg import daemon
    from msg.config import write_example

    write_example(
        named_layout.config, named_layout.data, 'https://example.org', postgres_dsn='service=main'
    )
    started = []
    monkeypatch.setattr(daemon, 'network_runtime', lambda settings: started.append(settings))
    monkeypatch.setattr(uvicorn, 'run', lambda *args, **kwargs: None)

    async def worker(app, **kwargs):
        return None

    monkeypatch.setattr(daemon, 'worker_loop', worker)
    assert daemon.main(['--instance', 'main', command]) == 0
    assert len(started) == 1
    assert started[0].server.content_dir == named_layout.data / 'git/content'
    assert started[0].root_private_dir == named_layout.root


def test_explicit_config_dir_preserves_legacy_storage_paths(monkeypatch, named_layout):
    from msg import application, daemon
    from msg.config import write_example

    legacy_data = named_layout.data.parent / 'legacy'
    write_example(
        named_layout.config, legacy_data, 'https://example.org', postgres_dsn='service=legacy'
    )
    monkeypatch.setattr(application, 'Application', lambda settings: settings)
    settings = daemon.load_application(named_layout.config)
    assert settings.server.content_dir == legacy_data / 'git/content'
    with pytest.raises(Failure, match='instance_storage_layout_mismatch'):
        daemon.load_application(named_layout.config, layout=named_layout)


def test_legal_foo_root_instance_does_not_conflict_with_its_own_private_root(tmp_path):
    from msg import daemon
    from msg.config import write_example

    assert ServerPaths.for_instance('foo-root').config == Path('/etc/msgd/foo-root')
    config = tmp_path / 'etc/foo-root'
    data = tmp_path / 'data/foo-root'
    settings = write_example(config, data, 'https://example.org', postgres_dsn='service=foo-root')
    layout = ServerPaths(
        config, data, settings.root_private_dir, tmp_path / 'cache', tmp_path / 'run'
    )
    layout.root.mkdir(parents=True)
    (layout.root / 'key.json').touch(mode=0o600)
    daemon.require_instance_layout(settings, layout)


@pytest.mark.parametrize('field', ['content', 'repositories', 'blobs', 'staging', 'service_keys'])
def test_named_runtime_rejects_each_storage_path_outside_its_layout(named_layout, field):
    from msg import daemon
    from msg.config import load_settings, write_example

    write_example(
        named_layout.config, named_layout.data, 'https://example.org', postgres_dsn='service=main'
    )
    config = named_layout.config / 'msgd.toml'
    config.write_text(
        '\n'.join(
            f'{field} = "/var/lib/msgd/shared"' if line.startswith(field + ' = ') else line
            for line in config.read_text().splitlines()
        )
    )
    with pytest.raises(Failure, match='instance_storage_layout_mismatch'):
        daemon.require_instance_layout(load_settings(named_layout.config), named_layout)


@pytest.mark.parametrize(
    'artifact',
    [
        'key.json',
        'root/key.json',
        'initialization.pending',
        'rotation.pending.json',
        'history',
        '.rotation.lock',
        'root/rotation.pending.json',
        'root/history',
    ],
)
def test_instance_preparation_rejects_legacy_root_collision_before_any_permission_change(
    tmp_path,
    artifact,
):
    # Substitute every absolute deployment path and all mutating tools. This only
    # probes a disposable filesystem; it never changes users or real system paths.
    base = tmp_path / 'host'
    config = base / 'etc/msgd/foo-root'
    data = base / 'var/lib/msgd/foo-root'
    protected = base / 'var/lib/private/msgd/foo-root/root'
    for directory in (config, data, data / 'service', protected):
        directory.mkdir(parents=True)
    key = config / artifact
    key.parent.mkdir(exist_ok=True)
    expected_mode = 0o700 if key.name == 'history' else 0o600
    if key.name == 'history':
        key.mkdir(mode=expected_mode)
    else:
        key.touch(mode=expected_mode)
    commands = tmp_path / 'commands'
    commands.mkdir()
    mutations = tmp_path / 'mutations.log'
    for name in ('id', 'getent', 'useradd', 'install', 'chown', 'chmod', 'python'):
        program = '#!/bin/sh\n'
        if name == 'id':
            program += 'printf "0\\n"\n'
        elif name not in {'getent', 'python'}:
            program += f'printf "%s\\n" "{name} $*" >> "$MUTATIONS_LOG"\n'
        (commands / name).write_text(program)
        (commands / name).chmod(0o700)
    source = (Path(__file__).parents[1] / 'deploy/prepare-instance.sh').read_text()
    for parent in (
        '/etc/msgd',
        '/var/lib/private',
        '/var/lib/msgd',
        '/var/cache/msgd',
        '/run/msgd',
    ):
        source = source.replace(parent, str(base / parent.lstrip('/')))
    source = source.replace('/usr/lib/msgd/python3.15/bin/python3.15', str(commands / 'python'))
    script = tmp_path / 'prepare.sh'
    script.write_text(source)
    result = subprocess.run(
        ['sh', str(script), 'foo-root'],
        env={
            **os.environ,
            'PATH': str(commands) + os.pathsep + os.environ['PATH'],
            'MUTATIONS_LOG': str(mutations),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert 'Root private artifacts' in result.stderr
    assert not mutations.exists()
    assert stat.S_IMODE(key.stat().st_mode) == expected_mode


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
    with pytest.raises(SystemExit) as absent_origin:
        main(['--instance', 'main', 'init'])
    assert absent_origin.value.code == 2
    assert '--service-url' in capsys.readouterr().err
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


@pytest.mark.parametrize('artifact', ['rotation.pending.json', 'history', '.rotation.lock'])
@pytest.mark.parametrize('dangling', [False, True])
def test_partial_legacy_root_state_preserves_location_and_rejects_ambiguity(
    monkeypatch, tmp_path, artifact, dangling
):
    from msg import config

    monkeypatch.setattr(config, 'SERVER_CONFIG_DIR', tmp_path / 'etc')
    current = tmp_path / 'private/main/root'
    monkeypatch.setattr(
        config.ServerPaths, 'for_instance', lambda name: SimpleNamespace(root=current)
    )
    legacy = tmp_path / 'etc/main-root'
    legacy.mkdir(parents=True)
    marker = legacy / artifact
    if dangling:
        marker.symlink_to(legacy / 'missing')
    elif artifact == 'history':
        marker.mkdir(mode=0o700)
    else:
        marker.touch(mode=0o600)
    assert config.root_private_dir(tmp_path / 'etc/main') == legacy
    assert not current.exists()
    current.mkdir(parents=True)
    (current / 'key.json').touch(mode=0o600)
    with pytest.raises(Failure, match='ambiguous_root_private_directory'):
        config.root_private_dir(tmp_path / 'etc/main')


def test_named_runtime_rejects_partial_legacy_root_location(monkeypatch, named_layout):
    from msg import daemon
    from msg.config import load_settings, write_example

    write_example(
        named_layout.config, named_layout.data, 'https://example.org', postgres_dsn='service=main'
    )
    legacy = named_layout.config.parent / (named_layout.config.name + '-root')
    legacy.mkdir()
    (legacy / 'rotation.pending.json').touch(mode=0o600)
    with pytest.raises(Failure, match='instance_root_layout_mismatch'):
        daemon.require_instance_layout(load_settings(named_layout.config), named_layout)
