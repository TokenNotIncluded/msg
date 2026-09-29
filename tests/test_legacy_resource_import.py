import hashlib
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_service import NOW, call, register

from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.storage.legacy_resource_import import PURPOSE, approval_payload, import_content


def source(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.executescript((Path(__file__).parent / 'fixtures/legacy_sqlite_0221.sql').read_text())
        db.execute("INSERT INTO boards VALUES ('main','old board',0,1.0)")
        db.execute("INSERT INTO boards VALUES ('meta','reserved v4 name',0,1.0)")
        db.execute(
            "INSERT INTO posts(id,board,seq,body,created,updated,author_id,signature,sig_version) VALUES (17,'main',2,'old body',1,1,'old-id','historical-signature',2)"
        )
        db.execute(
            "INSERT INTO attachments(post_id,slot,name,content_type,data,nbytes,sha256,created,uploader_name,downloads) VALUES (17,0,'old.txt','text/plain',?,3,?,1,'old',0)",
            (b'abc', hashlib.sha256(b'abc').hexdigest()),
        )
    return path


async def setup(installed, tmp_path):
    app, root = installed
    key, uid, cert = await register(app, 'importer')
    result = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'migration'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    assert result.status == 'ok', result
    parent = result.resources[0].id
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(parent)
        await tx.replace(
            replace(resource, mode=0o700, generation=resource.generation + 1), resource.generation
        )
        generation = resource.generation + 1
    snapshot = source(tmp_path)
    approval = approval_payload(
        sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        service=app.settings.service_url,
        parent=parent,
        parent_generation=generation,
        operator=uid,
        identities={'old-id': uid},
        expires_at=wire(NOW + timedelta(hours=1)),
    )
    return app, root, snapshot, approval, key, cert


@pytest.mark.asyncio
async def test_real_pg_import_reads_content_and_preserves_signature_reference(installed, tmp_path):
    app, root, snapshot, approval, key, cert = await setup(installed, tmp_path)
    approval['identities'] = {'old-id': None}
    async with app.metadata.transaction(write=False) as tx:
        counts = {
            t: tx.one('SELECT count(*) FROM ' + t)[0]
            for t in ('credentials', 'certificates', 'jobs')
        }
    report = await import_content(
        app, snapshot, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
    )
    assert report['posts'] == report['attachments'] == 1
    assert report['url_map']['/main/17'] == report['url_map']['/main/2']
    assert '/file/1' in report['url_map']
    owner_read = await call(
        app,
        'discovery.get',
        {'id': report['url_map']['/main/17']},
        key=key,
        subject=approval['operator'],
        certs=(cert,),
    )
    assert owner_read.status == 'ok', owner_read
    public = await call(app, 'discovery.get', {'id': report['url_map']['/main/2']})
    assert public.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.setting('legacy-import-approval:' + approval['source_sha256'])['approval']
            == approval
        )
        resource = await tx.resource(report['url_map']['/main/2'])
        assert resource.mode == 0o600
        revision = await tx.revision(ResourceRef(id=resource.id, revision=resource.revision))
        assert await app.contents.read_bytes(revision.content) == b'old body'
        assert revision.signature is None
        assert len(revision.relations) == 1
        attached = await tx.revision(revision.relations[0].target)
        assert await app.contents.read_bytes(attached.content) == b'abc'
        record = tx.setting('legacy-provenance:' + resource.id)
        provenance = await tx.resource(record['resource'])
        history = await tx.revision(ResourceRef(id=provenance.id, revision=provenance.revision))
        saved = await app.contents.read_bytes(history.content)
        assert b'historical-signature' in saved
        assert b'"historical_author_target":null' in saved
        assert b'old-id' in saved
        assert {t: tx.one('SELECT count(*) FROM ' + t)[0] for t in counts} == counts
    with pytest.raises(Failure):
        await import_content(
            app, snapshot, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
        )


@pytest.mark.asyncio
async def test_missing_mapping_and_unsigned_changes_rejected(installed, tmp_path):
    app, root, snapshot, approval, key, cert = await setup(installed, tmp_path)
    approval['identities'] = {}
    with pytest.raises(Failure, match='legacy_identity_mapping_incomplete'):
        await import_content(
            app, snapshot, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
        )
    approval['identities'] = {'old-id': approval['operator']}
    signed = wire(root.sign(canonical(approval), purpose=PURPOSE))
    approval['parent'] = 'r_root'
    with pytest.raises(Failure):
        await import_content(app, snapshot, approval, signed)


@pytest.mark.asyncio
async def test_failure_rolls_back_pg_resources_and_manifest(installed, tmp_path, monkeypatch):
    import msg.storage.legacy_resource_import as module

    app, root, snapshot, approval, key, cert = await setup(installed, tmp_path)
    original = module.revise_resource

    async def fail_after_content(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('injected after CAS write')

    monkeypatch.setattr(module, 'revise_resource', fail_after_content)
    with pytest.raises(RuntimeError, match='injected after CAS write'):
        await import_content(
            app, snapshot, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
        )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT id FROM resources WHERE parent=?', (approval['parent'],)) is None
        assert tx.setting('legacy-import:' + approval['source_sha256']) is None
        assert tx.one("SELECT id FROM resources WHERE id LIKE 'legacy_%'") is None


def test_mapping_plan_exposes_counts_and_digest_only(tmp_path):
    from msg.storage.legacy_resource_import import identity_requirements

    snapshot = source(tmp_path)
    plan = identity_requirements(snapshot, hashlib.sha256(snapshot.read_bytes()).hexdigest(), {})
    assert plan['required_count'] == plan['missing_count'] == 1
    assert plan['unexpected_count'] == 0
    assert 'old-id' not in str(plan)
    assert plan['requirements_digest'].startswith('sha256:')
