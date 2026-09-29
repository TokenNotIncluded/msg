"""Release-only retirement preserves history and refuses dangling dependencies."""
import shutil
import re
from dataclasses import replace

import pytest

from msg.bootstrap import sync_system_sources, system_source_root
from msg.core.codec import wire
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from test_service import NOW, call, register


async def retirement(app, tmp_path):
    source = tmp_path / 'retirement-source'
    shutil.copytree(system_source_root(), source)
    (source / 'rules/recovery.md').unlink()
    for path in source.rglob('*.md'):
        text = path.read_text()
        if '/_rules/recovery' in text:
            text = re.sub(r'version: (\d+)', lambda match: 'version: ' + str(int(match[1])+1),
                          text, count=1)
            path.write_text('\n'.join(line for line in text.splitlines()
                                      if '/_rules/recovery' not in line) + '\n')
    async with app.metadata.transaction(write=False) as tx:
        row = tx.one('SELECT source_path,source_version,source_digest FROM system_sources '
                     'WHERE rule_id=?', ('msg.recovery',))
    return source, {'msg.recovery': {'source_path': row[0].removeprefix('docs/system/'),
                                    'version': row[1], 'digest': row[2]}}


@pytest.mark.asyncio
async def test_declared_retirement_preserves_history_is_idempotent_and_read_only(installed, tmp_path, monkeypatch):
    app, _ = installed
    source, declaration = await retirement(app, tmp_path)
    async with app.metadata.transaction(write=False) as tx:
        before = await tx.resource('r_rule_recovery')
        revision = await tx.revision(ResourceRef(id=before.id, revision=before.revision))
        original = await app.contents.read_bytes(revision.content)
    for _ in range(2):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx, app.contents, NOW, source_root=source,
                                      retirements=declaration, registry=app.registry)
        async with app.metadata.transaction(write=False) as tx:
            after = await tx.resource(before.id)
            assert after.state == 'archived'
            assert (after.revision, after.generation) == (before.revision, before.generation + 1)
            assert wire(await tx.revision(ResourceRef(id=before.id, revision=before.revision))) == wire(revision)
            assert await app.contents.read_bytes(revision.content) == original
    import msg.bootstrap as bootstrap
    from msg.application import Application
    monkeypatch.setattr(bootstrap, 'SOURCE_RETIREMENTS', declaration)
    monkeypatch.setattr(bootstrap, 'system_source_root', lambda: source)
    reloaded = Application(app.settings, clock=lambda: NOW)
    try:
        await reloaded.load()
        async with reloaded.metadata.transaction(write=False) as tx:
            assert (await tx.resource(before.id)).state == 'archived'
    finally:
        await reloaded.close()
    read = await call(app, 'discovery.get', {'id': before.id, 'revision': before.revision})
    assert read.status == 'ok', wire(read)
    assert read.data['content'].encode() == original
    key, subject, _ = await register(app, 'retirement-writer')
    for operation, args in (
        ('content.chmod', {'id': before.id, 'mode': '0777'}),
        ('content.post_edit', {'id': before.id, 'expected_revision': before.revision, 'body': 'replace'}),
    ):
        result = await call(app, operation, args, key=key, subject=subject)
        assert result.error.code == 'system_managed_resource', wire(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['version', 'digest', 'source_path'])
async def test_retirement_must_pin_current_release_metadata(installed, tmp_path, mismatch):
    app, _ = installed
    source, declaration = await retirement(app, tmp_path)
    declaration['msg.recovery'][mismatch] = {'version': 999, 'digest': 'sha256:' + '0' * 64,
                                            'source_path': 'rules/wrong.md'}[mismatch]
    with pytest.raises(Failure, match='^system_source_retirement_mismatch$'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx, app.contents, NOW, source_root=source,
                                      retirements=declaration, registry=app.registry)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource('r_rule_recovery')).state == 'active'


@pytest.mark.asyncio
@pytest.mark.parametrize('dependency', ['operation', 'index', 'source', 'registry_missing'])
async def test_retirement_rejects_active_dependencies(installed, tmp_path, dependency):
    app, _ = installed
    source, declaration = await retirement(app, tmp_path)
    registry = app.registry
    if dependency == 'operation':
        key = ('discovery.get', 1)
        registry._operations[key] = replace(registry.operation(*key), requires_rules=('msg.recovery',))
    elif dependency == 'index':
        with (source / 'rules/_index.md').open('a') as handle:
            handle.write('\n[Recovery](/_rules/recovery)\n')
    elif dependency == 'source':
        with (source / 'rules/security.md').open('a') as handle:
            handle.write('\n<!-- requires_rules: msg.recovery -->\n')
    else:
        registry = None
    with pytest.raises(Failure, match='^system_source_retirement_referenced$'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx, app.contents, NOW, source_root=source,
                                      retirements=declaration, registry=registry)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource('r_rule_recovery')).state == 'active'


@pytest.mark.asyncio
async def test_fresh_install_does_not_invent_retired_history(installed, tmp_path, pg_dsn, monkeypatch):
    import msg.bootstrap as bootstrap
    from msg.admin.root import _provision
    from msg.application import Application
    from msg.config import write_example

    source, declaration = await retirement(installed[0], tmp_path)
    monkeypatch.setattr(bootstrap, 'SOURCE_RETIREMENTS', declaration)
    monkeypatch.setattr(bootstrap, 'system_source_root', lambda: source)
    app = Application(write_example(tmp_path / 'fresh-config', tmp_path / 'fresh-data',
                                   postgres_dsn=pg_dsn), clock=lambda: NOW)
    try:
        await _provision(app, 'test-retirement-passphrase')
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT id FROM resources WHERE id=?', ('r_rule_recovery',)) is None
            assert tx.one('SELECT resource_id FROM system_sources WHERE rule_id=?', ('msg.recovery',)) is None
            assert tx.one('SELECT id FROM resources WHERE id=?', ('r_rule_security',)) is not None
    finally:
        await app.close()


def test_registry_freeze_rejects_dependency_on_release_retired_rule(monkeypatch):
    import msg.bootstrap as bootstrap
    from test_registry_operation_rules import assemble

    monkeypatch.setattr(bootstrap, 'SOURCE_RETIREMENTS', {'msg.recovery': {}})
    registry = assemble('recovery.custom', requires_rules=('msg.recovery',))
    with pytest.raises(Failure, match='^dangling_requires_rules$'):
        registry.freeze()
    assert not registry.frozen


@pytest.mark.asyncio
async def test_retirement_cannot_implicitly_reactivate_archived_source(installed, tmp_path):
    app, _ = installed
    source, declaration = await retirement(app, tmp_path)
    async with app.metadata.transaction(write=True) as tx:
        await sync_system_sources(tx, app.contents, NOW, source_root=source,
                                  retirements=declaration, registry=app.registry)
    shutil.copyfile(system_source_root() / 'rules/recovery.md', source / 'rules/recovery.md')
    with pytest.raises(Failure, match='^system_source_retirement_required$'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx, app.contents, NOW, source_root=source,
                                      registry=app.registry)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource('r_rule_recovery')).state == 'archived'


@pytest.mark.asyncio
@pytest.mark.parametrize(('change', 'code'), [
    ('unknown', 'system_source_invalid_retirement'),
    ('mandatory', 'system_source_invalid_retirement'),
    ('malformed', 'system_source_invalid_retirement'),
    ('source_present', 'system_source_retirement_source_present'),
    ('pointer_drift', 'system_source_pointer_drift'),
])
async def test_retirement_refuses_invalid_declarations_or_source_drift(installed, tmp_path, change, code):
    app, _ = installed
    source, declaration = await retirement(app, tmp_path)
    if change == 'unknown':
        declaration['msg.unknown'] = declaration.pop('msg.recovery')
    elif change == 'mandatory':
        declaration['msg.rules.index'] = declaration.pop('msg.recovery')
    elif change == 'malformed':
        declaration['msg.recovery']['version'] = True
    elif change == 'source_present':
        shutil.copyfile(system_source_root() / 'rules/recovery.md', source / 'rules/recovery.md')
    elif change == 'pointer_drift':
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource('r_rule_recovery')
            await tx.replace(replace(resource, revision=None, generation=resource.generation+1),
                             resource.generation)
    with pytest.raises(Failure, match='^' + code + '$'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx, app.contents, NOW, source_root=source,
                                      retirements=declaration, registry=app.registry)
