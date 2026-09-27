"""Release-owned rules and editable wiki have separate authority boundaries."""
import shutil
import re
from dataclasses import replace

import pytest
import tiktoken
import httpx

from msg.core.codec import digest, wire
from msg.core.errors import Failure
from msg.transports.http import create_app
from test_service import NOW, call, register


def test_source_rule_token_budgets_and_index_links():
    from msg.bootstrap import RULE_NAMES, system_source_root
    root=system_source_root()
    encode=tiktoken.get_encoding('cl100k_base').encode
    assert len(encode((root/'AGENTS.md').read_text())) <= 200
    index=(root/'rules'/'_index.md').read_text()
    assert len(encode(index)) <= 400
    for name in RULE_NAMES:
        source=(root/'rules'/(name+'.md')).read_text()
        assert len(encode(source)) <= 200
        assert f'](/_rules/{name})' in index


@pytest.mark.asyncio
async def test_short_bootstrap_rule_index_shards_and_editable_wiki(installed):
    app, _ = installed
    intro = await call(app, 'discovery.get', {'id': '/AGENTS.md'})
    index = await call(app, 'discovery.get', {'id': '/_rules/_index.md'})
    assert intro.status == index.status == 'ok', (wire(intro), wire(index))
    assert '/_rules' in intro.data['content'] and '/wiki' in intro.data['content']
    assert len(intro.data['content'].split()) < 150
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        index_path=await http.get('/_rules/_index.md')
        index_default=await http.get('/_rules')
        index_slash=await http.get('/_rules/')
        assert index_path.status_code==index_default.status_code==index_slash.status_code==200
        assert index_path.content==index_default.content==index_slash.content
        assert index_path.headers['etag']==index_default.headers['etag']==index_slash.headers['etag']
        assert 'location' not in index_default.headers
        assert (await http.post('/_rules')).status_code==405
    seen=set()
    for name in ('identity','read-write','auth','topics','files','recovery','security','protocol'):
        shard = await call(app, 'discovery.get', {'id': '/_rules/' + name})
        assert shard.status == 'ok' and 'rule_id:' in shard.data['content'], name
        assert '/_rules/' + name in index.data['content']
        rule_id=re.search(r'rule_id: ([a-z][a-z0-9.\-]*)',shard.data['content']).group(1)
        assert rule_id not in seen
        seen.add(rule_id)
    key, subject, _ = await register(app, 'wiki-editor')
    before = digest(index.data['content'])
    article = await call(app, 'content.post_create', {'parent': '/wiki',
        'body': 'Community explanation, not platform authority.'}, key=key, subject=subject)
    assert article.status == 'ok', wire(article)
    edited = await call(app, 'content.post_edit', {'id': article.resources[0].id,
        'expected_revision': article.resources[0].revision,
        'body': 'A corrected community explanation.'}, key=key, subject=subject,
        expected=((article.resources[0].id,article.data['generation']),))
    assert edited.status == 'ok', wire(edited)
    unchanged = await call(app, 'discovery.get', {'id': '/_rules/_index.md'})
    assert digest(unchanged.data['content']) == before
    for op, args in (('content.chmod', {'id': '/AGENTS.md', 'mode': '0777'}),
                     ('content.move', {'id': '/_rules/_index.md', 'parent': '/wiki'}),
                     ('content.post_edit', {'id': '/AGENTS.md', 'expected_revision': 'unused', 'body': 'replace'}),
                     ('content.post_create', {'parent': '/_rules', 'body': 'fake rule'})):
        expected=(('r_rule_index',index.data['generation']),) if op=='content.move' else ()
        result = await call(app, op, args, key=key, subject=subject, expected=expected)
        assert result.status == 'error' and result.error.code == 'system_managed_resource', (op, wire(result))


@pytest.mark.asyncio
async def test_per_file_upgrade_changes_only_edited_revision_and_rejects_deleted_source(installed,tmp_path):
    from msg.bootstrap import system_source_root, sync_system_sources
    app, _ = installed
    source = tmp_path / 'system'
    shutil.copytree(system_source_root(), source)
    async with app.metadata.transaction(write=False) as tx:
        agents_before = (await tx.resource('r_agents')).revision
        identity_before = (await tx.resource('r_rule_identity')).revision
    identity = source / 'rules' / 'identity.md'
    identity.write_text(identity.read_text().replace('version: 1', 'version: 2') + '\nNew release guidance.\n')
    async with app.metadata.transaction(write=True) as tx:
        await sync_system_sources(tx,app.contents,NOW,source_root=source)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource('r_agents')).revision == agents_before
        current = await tx.resource('r_rule_identity')
        assert current.revision != identity_before
        assert (await tx.revision(__import__('msg.core.models',fromlist=['ResourceRef']).ResourceRef(
            id=current.id,revision=current.revision))).parents == (identity_before,)
        row=tx.one('SELECT source_kind,source_version,source_digest FROM system_sources WHERE resource_id=?',
                   ('r_rule_identity',))
        assert row[0] == 'release' and row[1] == 2 and row[2].startswith('sha256:')
    identity.write_text(identity.read_text() + '\nChanged without a version bump.\n')
    with pytest.raises(Failure, match='system_source_version_required'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx,app.contents,NOW,source_root=source)
    identity.write_text(identity.read_text().replace('\nChanged without a version bump.\n',''))
    (source / 'rules' / 'files.md').unlink()
    with pytest.raises(Failure, match='system_source_deleted_requires_migration'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx,app.contents,NOW,source_root=source)


@pytest.mark.asyncio
async def test_new_application_load_syncs_changed_packaged_release(installed,tmp_path,monkeypatch):
    import msg.bootstrap as bootstrap
    from msg.application import Application
    app, _ = installed
    source=tmp_path/'system'
    shutil.copytree(bootstrap.system_source_root(),source)
    rules=source/'rules'/'topics.md'
    rules.write_text(rules.read_text().replace('version: 1','version: 2')+'\nRelease update.\n')
    monkeypatch.setattr(bootstrap,'system_source_root',lambda:source)
    async with app.metadata.transaction(write=False) as tx:
        before=(await tx.resource('r_rule_topics')).revision
    next_app=Application(app.settings,clock=lambda:NOW)
    try:
        await next_app.load()
        async with next_app.metadata.transaction(write=False) as tx:
            changed=(await tx.resource('r_rule_topics')).revision
            assert changed!=before
            count=tx.one('SELECT COUNT(*) FROM revisions')[0]
    finally:
        await next_app.close()
    again=Application(app.settings,clock=lambda:NOW)
    try:
        await again.load()
        async with again.metadata.transaction(write=False) as tx:
            assert (await tx.resource('r_rule_topics')).revision==changed
            assert tx.one('SELECT COUNT(*) FROM revisions')[0]==count
    finally:
        await again.close()
    (source/'rules'/'files.md').unlink()
    missing=Application(app.settings,clock=lambda:NOW)
    try:
        with pytest.raises(Failure,match='system_source_deleted_requires_migration'):
            await missing.load()
    finally:
        await missing.close()


@pytest.mark.asyncio
async def test_rule_sync_rejects_current_pointer_drift(installed):
    from msg.bootstrap import sync_system_sources
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        rule=await tx.resource('r_rule_identity')
        await tx.replace(replace(rule,revision=None,generation=rule.generation+1),
                         rule.generation)
    with pytest.raises(Failure,match='system_source_pointer_drift'):
        async with app.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx,app.contents,NOW)
