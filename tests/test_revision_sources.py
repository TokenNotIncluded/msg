"""Release provenance lives on immutable revisions without rewriting legacy ones."""
import shutil

import pytest

from msg.core.codec import canonical, decode, wire
from msg.core.models import Revision
from msg.bootstrap import system_source_root, sync_system_sources
from test_service import NOW, call, register


def test_legacy_revision_decodes_without_new_source_fields():
    legacy={'format_version':1,'id':'v_legacy','resource_id':'r_old','parents':[],
            'content':{'digest':'sha256:'+'0'*64,'size':0,'media_type':'text/plain'},
            'relations':[],'actor':'u_old','subject':'u_old','author':'u_old',
            'created_at':'2026-09-27T00:00:00.000000Z','manifest_digest':'sha256:'+'1'*64,
            'signature':None}
    decoded=decode(Revision,legacy)
    assert decoded.source_kind is None and decoded.change_note is None
    assert canonical(decoded)==canonical(legacy)


@pytest.mark.asyncio
async def test_release_source_fields_history_cursor_and_exact_diff(installed,tmp_path):
    app, _ = installed
    rid='r_rule_identity'
    async with app.metadata.transaction(write=False) as tx:
        first=(await tx.resource(rid)).revision
        source=await tx.revision(__import__('msg.core.models',fromlist=['ResourceRef']).ResourceRef(id=rid))
        assert (source.source_kind,source.source_version,source.source_digest)==(
            'release',1,source.content.digest)
    copied=tmp_path/'system'
    shutil.copytree(system_source_root(),copied)
    path=copied/'rules'/'identity.md'
    path.write_text(path.read_text().replace('version: 1','version: 2')+
                    '\n<!-- change_note: Clarified key ownership. -->\nA new sentence.\n')
    async with app.metadata.transaction(write=True) as tx:
        await sync_system_sources(tx,app.contents,NOW,source_root=copied)
    history=await call(app,'discovery.get',{'id':rid,'view':'history','limit':1})
    assert history.status=='ok' and history.data['cursor']
    older=await call(app,'discovery.get',{'id':rid,'view':'history','limit':1,
        'cursor':history.data['cursor']})
    records=(*history.data['revisions'],*older.data['revisions'])
    assert {item['source_version'] for item in records}=={1,2}
    assert any(item.get('change_note')=='Clarified key ownership.' for item in records)
    current=(await call(app,'discovery.get',{'id':rid})).data['revision']
    diff=await call(app,'discovery.diff_view',{'id':rid,'known_revision':first})
    assert diff.status=='ok' and diff.data['from']['revision']==first
    assert diff.data['to']['revision']==current and '+A new sentence.' in diff.data['diff']
    assert diff.data['to_source']['source_version']==2


@pytest.mark.asyncio
async def test_user_cannot_forge_release_source_and_rule_links_resolve(installed):
    app, _ = installed
    key,subject,_=await register(app,'source-user')
    forged=await call(app,'content.post_create',{'parent':'/main','body':'a post',
        'source_kind':'release','source_version':999},key=key,subject=subject)
    assert forged.status=='error' and forged.error.code=='schema_validation'
    for operation in app.registry.operations('network'):
        described=app.registry.describe(operation)
        links=described['requires_rules']
        assert links
        for link in links:
            assert link['rule_id'].startswith('msg.') and link['path'].startswith('/_rules/')
            rule=await call(app,'discovery.get',{'id':link['path']})
            assert rule.status=='ok' and link['rule_id'] in rule.data['content']
    schema=await call(app,'discovery.schema',{'operation':'identity.register'})
    assert schema.status=='ok' and schema.data['operation']['requires_rules']


@pytest.mark.asyncio
async def test_history_cursor_rechecks_principal_and_current_visibility(installed):
    app, _ = installed
    key,subject,_=await register(app,'history-owner')
    created=await call(app,'content.post_create',{'parent':'/main','body':'first'},
                       key=key,subject=subject)
    rid=created.resources[0].id
    edited=await call(app,'content.post_edit',{'id':rid,
        'expected_revision':created.resources[0].revision,'body':'second'},
        key=key,subject=subject,expected=((rid,created.data['generation']),))
    assert edited.status=='ok'
    page=await call(app,'discovery.get',{'id':rid,'view':'history','limit':1})
    assert page.status=='ok' and page.data['cursor']
    wrong=await call(app,'discovery.get',{'id':rid,'view':'history','limit':1,
        'cursor':page.data['cursor']},key=key,subject=subject)
    assert wrong.status=='error' and wrong.error.code=='cursor_principal_mismatch'
    hidden=await call(app,'content.chmod',{'id':rid,'mode':'0600'},key=key,subject=subject,
                      expected=((rid,edited.data['generation']),))
    assert hidden.status=='ok'
    stale=await call(app,'discovery.get',{'id':rid,'view':'history','limit':1,
        'cursor':page.data['cursor']})
    assert stale.status=='error'
