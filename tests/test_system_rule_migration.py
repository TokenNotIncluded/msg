"""Release rule identity survives an explicitly declared source relocation."""
import shutil

import pytest
from test_service import NOW

from msg.bootstrap import sync_system_sources, system_source_root
from msg.core.errors import Failure


@pytest.mark.asyncio
@pytest.mark.parametrize("container", [tuple, list])
async def test_source_move_preserves_public_resource_and_revision(installed,tmp_path,container):
    app,_=installed
    source=tmp_path/'system'
    shutil.copytree(system_source_root(),source)
    original=source/'rules'/'identity.md'
    moved=source/'rules'/'moved'/'identity.md'
    moved.parent.mkdir()
    original.rename(moved)
    paths={'msg.identity':'rules/moved/identity.md'}
    declaration={'msg.identity':container(('rules/identity.md','rules/moved/identity.md'))}
    async with app.metadata.transaction(write=False) as tx:
        before=await tx.resource('r_rule_identity')
    with pytest.raises(Failure,match='system_source_migration_required'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx,app.contents,NOW,source_root=source,source_paths=paths)
    async with app.metadata.transaction(write=True) as tx:
        await sync_system_sources(tx,app.contents,NOW,source_root=source,
                                  source_paths=paths,migrations=declaration)
    async with app.metadata.transaction(write=False) as tx:
        after=await tx.resource('r_rule_identity')
        row=tx.one('SELECT source_path,rule_id,revision_id FROM system_sources WHERE resource_id=?',
                   ('r_rule_identity',))
        assert (after.id,after.revision,after.generation)==(
            before.id,before.revision,before.generation)
        assert row==('docs/system/rules/moved/identity.md','msg.identity',before.revision)
    # The same release declaration remains valid on every subsequent load.
    async with app.metadata.transaction(write=True) as tx:
        await sync_system_sources(tx,app.contents,NOW,source_root=source,
                                  source_paths=paths,migrations=declaration)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource('r_rule_identity')).revision==before.revision


@pytest.mark.asyncio
async def test_unknown_duplicate_and_dangling_rule_sources_are_rejected(installed,tmp_path):
    app,_=installed
    source=tmp_path/'system'
    shutil.copytree(system_source_root(),source)
    extra=source/'rules'/'extra.md'
    extra.write_text('<!-- rule_id: msg.unregistered; version: 1 -->\n')
    with pytest.raises(Failure,match='unknown_rule_id'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx,app.contents,NOW,source_root=source)
    extra.write_text((source/'rules'/'identity.md').read_text())
    with pytest.raises(Failure,match='duplicate_rule_id'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx,app.contents,NOW,source_root=source)
    extra.unlink()
    identity=source/'rules'/'identity.md'
    identity.write_text(identity.read_text()+'\n<!-- requires_rules: msg.missing -->\n')
    with pytest.raises(Failure,match='dangling_requires_rules'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx,app.contents,NOW,source_root=source)
