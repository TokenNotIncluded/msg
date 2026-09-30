"""Release page updates are atomic, idempotent, and preserve user revisions."""

from dataclasses import replace

import pytest

from msg import bootstrap
from msg.core.codec import digest
from msg.core.models import ResourceRef


@pytest.mark.asyncio
async def test_release_page_upgrade_is_atomic_and_idempotent(installed, monkeypatch):
    app, _ = installed
    before = bootstrap.ROOT_WEB_SAMPLE
    desired = before.replace('Start with a message.'.encode(), 'Continue with a message.'.encode())
    assert desired != before
    monkeypatch.setattr(bootstrap, 'ROOT_WEB_SAMPLE', desired)
    async with app.metadata.transaction(write=True) as tx:
        site = await tx.resource('w_root_web')
        file = await tx.resource('f_root_web_index')
        tx.set_setting('root_web_release_digest', digest(before))
        await bootstrap.sync_root_web_sample(tx, app.contents, app.clock())
        changed_site = await tx.resource(site.id)
        changed_file = await tx.resource(file.id)
        assert changed_site.generation == site.generation + 1
        assert changed_file.generation == file.generation + 1
        page = await tx.revision(ResourceRef(id=file.id))
        assert await app.contents.read_bytes(page.content) == desired
        from msg.core.codec import loads

        manifest = loads(
            await app.contents.read_bytes((await tx.revision(ResourceRef(id=site.id))).content)
        )
        assert manifest['entries']['index.html']['revision'] == page.id
        await bootstrap.sync_root_web_sample(tx, app.contents, app.clock())
        assert (await tx.resource(site.id)).generation == changed_site.generation
        assert (await tx.resource(file.id)).generation == changed_file.generation
        # Returning to an already recorded release must not duplicate immutable IDs.
        monkeypatch.setattr(bootstrap, 'ROOT_WEB_SAMPLE', before)
        await bootstrap.sync_root_web_sample(tx, app.contents, app.clock())
        assert (await tx.resource(site.id)).revision == site.revision
        assert (await tx.resource(file.id)).revision == file.revision


@pytest.mark.asyncio
@pytest.mark.parametrize('resource_id', ['w_root_web', 'f_root_web_index'])
async def test_release_page_preserves_custom_revision(installed, monkeypatch, resource_id):
    app, _ = installed
    monkeypatch.setattr(bootstrap, 'ROOT_WEB_SAMPLE', b'<h1>next release</h1>')
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(resource_id)
        revision = await tx.revision(ResourceRef(id=resource_id))
        custom = replace(revision, id='v_custom_sample')
        await tx.append_revision(custom)
        await tx.replace(
            replace(resource, revision=custom.id, generation=resource.generation + 1),
            resource.generation,
        )
        site_before = await tx.resource('w_root_web')
        file_before = await tx.resource('f_root_web_index')
        await bootstrap.sync_root_web_sample(tx, app.contents, app.clock())
        assert await tx.resource('w_root_web') == site_before
        assert await tx.resource('f_root_web_index') == file_before
