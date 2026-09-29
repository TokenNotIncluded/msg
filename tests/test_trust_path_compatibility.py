import pytest

from msg.application import Application
from msg.config import load_settings, write_example
from msg.core.codec import canonical, loads
from msg.core.errors import Failure


def test_new_layout_and_legacy_configuration(tmp_path):
    write_example(tmp_path, tmp_path / 'data')
    settings = load_settings(tmp_path)
    assert settings.trust_file == tmp_path / 'trust' / 'root.crt'
    assert (tmp_path / 'plugins.d').is_dir()
    # Reserved contents are inert, including Python source; no fragment syntax exists.
    (tmp_path / 'plugins.d' / 'untrusted.py').write_text("raise RuntimeError('must not execute')\n")
    load_settings(tmp_path)
    (tmp_path / 'plugins.d' / 'untrusted.py').unlink()
    (tmp_path / 'plugins.d').rmdir()
    assert load_settings(tmp_path).config_dir == tmp_path
    assert not (tmp_path / 'plugins.d').exists()
    settings.trust_file.parent.mkdir()
    legacy = tmp_path / 'trust' / 'root.json'
    legacy.write_bytes(b'{"version":1}')
    assert load_settings(tmp_path).trust_file == legacy
    modern = legacy.with_name('root.crt')
    modern.write_bytes(b'{ "version" : 1 }\n')
    assert settings.trust_file == modern
    modern.write_bytes(b'{"version":2}')
    with pytest.raises(Failure, match='root_trust_alias_mismatch'):
        _ = settings.trust_file
    assert legacy.read_bytes() == b'{"version":1}'


@pytest.mark.parametrize('unsafe', ['symlink', 'writable'])
def test_reserved_plugin_directory_rejects_unsafe_paths(tmp_path, unsafe):
    write_example(tmp_path, tmp_path / 'data')
    directory = tmp_path / 'plugins.d'
    directory.rmdir()
    if unsafe == 'symlink':
        destination = tmp_path / 'elsewhere'
        destination.mkdir()
        directory.symlink_to(destination, target_is_directory=True)
    else:
        directory.mkdir()
        directory.chmod(0o777)
    with pytest.raises(Failure, match='unsafe_plugins_directory'):
        load_settings(tmp_path)


@pytest.mark.asyncio
async def test_legacy_root_json_really_loads_and_conflict_refuses(installed):
    app, root = installed
    modern = app.settings.trust_file
    assert modern.name == 'root.crt'
    legacy = modern.with_name('root.json')
    modern.rename(legacy)
    await app.load()
    assert app.certificates.root_public_key == root.public_key
    modern.write_bytes(legacy.read_bytes())
    await app.load()
    changed = loads(modern.read_bytes())
    changed['public_key'] = 'changed'
    modern.write_bytes(canonical(changed))
    with pytest.raises(Failure, match='root_trust_alias_mismatch'):
        await app.load()


@pytest.mark.asyncio
async def test_dual_alias_rotation_interruption_is_closed_and_resumable(installed, monkeypatch):
    from test_root_rotation_resume import NEW_PIN, old_envelope

    from msg.admin import rotation
    from msg.security.crypto import Ed25519Signer

    app, root = installed
    old_envelope(app, root)
    modern = app.settings.trust_file
    legacy = modern.with_name('root.json')
    legacy.write_bytes(modern.read_bytes())
    successor = Ed25519Signer.generate()
    journal = rotation.prepare(app, successor, NEW_PIN, old_signer=root, operator='test')
    real_write = rotation.durable_write

    def interrupt(path, data, **kwargs):
        if path == legacy:
            raise OSError('alias interruption')
        return real_write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rotation, 'durable_write', interrupt)
        with pytest.raises(OSError, match='alias interruption'):
            await rotation.complete(app, journal, pin=NEW_PIN)
    assert rotation.journal_path(app).exists()
    with pytest.raises(Failure, match='root_trust_alias_mismatch'):
        await app.load()
    fresh = Application(app.settings, clock=app.clock)
    try:
        await rotation.complete(fresh, journal, pin=NEW_PIN)
        assert modern.read_bytes() == legacy.read_bytes()
        await fresh.load()
        assert fresh.certificates.root_public_key == successor.public_key
    finally:
        await fresh.close()


@pytest.mark.parametrize('alias', ['root.crt', 'root.json'])
def test_trust_alias_symlinks_fail_closed(tmp_path, alias):
    write_example(tmp_path, tmp_path / 'data')
    directory = tmp_path / 'trust'
    directory.mkdir()
    (directory / alias).symlink_to(tmp_path / 'missing')
    with pytest.raises(Failure, match='unsafe_root_trust_path'):
        _ = load_settings(tmp_path).trust_file


@pytest.mark.asyncio
async def test_rotation_refuses_unpinned_alias_before_database_commit(installed):
    from test_root_rotation_resume import NEW_PIN, old_envelope

    from msg.admin import rotation
    from msg.security.crypto import Ed25519Signer

    app, root = installed
    old_envelope(app, root)
    journal = rotation.prepare(
        app, Ed25519Signer.generate(), NEW_PIN, old_signer=root, operator='test'
    )
    modern = app.settings.trust_file
    legacy = modern.with_name('root.json')
    legacy.write_bytes(b'{"version":1,"public_key":"substituted"}')
    with pytest.raises(Failure, match='rotation_trust_changed'):
        await rotation.complete(app, journal, pin=NEW_PIN)
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.setting('active_root_certificate', 'cert_root')
            == journal['old_certificate']['resource_id']
        )
    assert rotation.journal_path(app).exists()


@pytest.mark.asyncio
async def test_legacy_rotation_preserves_legacy_path(installed):
    from test_root_rotation_resume import NEW_PIN, old_envelope

    from msg.admin import rotation
    from msg.security.crypto import Ed25519Signer

    app, root = installed
    old_envelope(app, root)
    modern = app.settings.trust_file
    legacy = modern.with_name('root.json')
    modern.rename(legacy)
    successor = Ed25519Signer.generate()
    await rotation.complete(
        app,
        rotation.prepare(app, successor, NEW_PIN, old_signer=root, operator='test'),
        pin=NEW_PIN,
    )
    assert legacy.is_file() and not modern.exists()
    await app.load()
    assert app.certificates.root_public_key == successor.public_key
