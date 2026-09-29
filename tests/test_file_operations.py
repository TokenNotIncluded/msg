"""The file vocabulary uses the same resources, revisions and authorization."""
import pytest
from msg.core.codec import b64, wire
from test_service import call, register


@pytest.mark.asyncio
async def test_file_write_read_copy_conflicts_and_directory_permissions(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'file-contract')
    stranger, other, _ = await register(app, 'file-outsider')
    parent = '/@file-contract/files'
    created = await call(app, 'file.create', {'parent':parent,'name':'a.txt',
        'data':b64(b'old'),'media_type':'text/plain'}, key=key, subject=owner)
    assert created.status == 'ok', wire(created)
    rid = created.resources[0].id
    duplicate = await call(app, 'file.create', {'parent':parent,'name':'a.txt',
        'data':b64(b'duplicate')}, key=key, subject=owner)
    assert duplicate.status == 'error'
    args = {'id':rid,'base_revision':created.resources[0].revision,'data':b64(b'new')}
    denied = await call(app, 'file.write', args, key=stranger, subject=other,
        expected=((rid,created.data['generation']),))
    assert denied.status == 'error' and denied.error.code == 'permission_denied', wire(denied)
    written = await call(app, 'file.write', args, key=key, subject=owner,
        expected=((rid,created.data['generation']),))
    assert written.status == 'ok', wire(written)
    stale = await call(app, 'file.write', args, key=key, subject=owner,
        expected=((rid,written.data['generation']),))
    assert stale.status == 'error' and stale.error.code == 'revision_conflict', wire(stale)
    read = await call(app, 'file.read', {'id':rid}, key=key, subject=owner)
    assert read.status == 'ok' and read.data['content'] == 'new', wire(read)
    old = await call(app, 'file.read', {'id':rid,'revision':created.resources[0].revision},key=key,subject=owner)
    assert old.status == 'ok' and old.data['content'] == 'old', wire(old)
    copy = await call(app, 'file.copy', {'parent':parent,'name':'b.txt','source':{'id':rid}},key=key,subject=owner)
    assert copy.status == 'ok', wire(copy)
    stat = await call(app, 'file.stat', {'id':rid},key=key,subject=owner)
    assert stat.status == 'ok' and stat.data['generation'] == written.data['generation'], wire(stat)
    folder = await call(app, 'file.mkdir', {'parent':parent,'name':'nested'},key=key,subject=owner)
    assert folder.status == 'ok', wire(folder)
    moved = await call(app, 'file.move', {'id':rid,'parent':folder.resources[0].id},key=key,subject=owner,
        expected=((rid,written.data['generation']),))
    assert moved.status == 'ok', wire(moved)
    listing = await call(app, 'file.list', {'parent':folder.resources[0].id},key=key,subject=owner)
    assert listing.status == 'ok' and [item['id'] for item in listing.data['items']] == [rid], wire(listing)
    deleted = await call(app, 'file.delete', {'id':rid},key=key,subject=owner,
        expected=((rid,moved.data['generation']),))
    assert deleted.status == 'ok' and deleted.data['state'] == 'archived', wire(deleted)


@pytest.mark.asyncio
async def test_existing_operation_limited_key_does_not_gain_file_alias(installed):
    from msg.core.codec import canonical
    from msg.core.models import Scope
    from msg.security.capabilities import grant_for
    from msg.security.crypto import Ed25519Signer
    app, _ = installed
    owner_key, owner, _ = await register(app, 'file-limited')
    key = Ed25519Signer.generate()
    grant = grant_for(app.registry.capability('resource.basic'),
        scope=Scope(resource_id=owner,descendants=True), operations=('content.file_put@1',))
    proof = key.sign(canonical({'subject_id':owner,'public_key':b64(key.public_key)}),purpose='key-add')
    added = await call(app,'identity.key_add',{'public_key':b64(key.public_key),
        'possession_proof':wire(proof),'ceiling':wire((grant,))},key=owner_key,subject=owner)
    assert added.status == 'ok', wire(added)
    args = {'parent':'/@file-limited/files','name':'old.txt','data':b64(b'bytes')}
    allowed = await call(app,'content.file_put',args,key=key,subject=owner)
    assert allowed.status == 'ok', wire(allowed)
    denied = await call(app,'file.create',{**args,'name':'alias.txt'},key=key,subject=owner)
    assert denied.status == 'error' and denied.error.code == 'credential_ceiling', wire(denied)


@pytest.mark.asyncio
async def test_file_patch_and_atomic_batch_preserve_single_publication(installed):
    from test_batch import packet
    app, _ = installed
    key, owner, _ = await register(app,'file-batch')
    args = {'parent':'/@file-batch/files','name':'edit.txt','data':b64(b'old'),'media_type':'text/plain'}
    created = await call(app,'file.create',args,key=key,subject=owner)
    rid = created.resources[0].id
    patched = await call(app,'file.patch',{'id':rid,'base_revision':created.resources[0].revision,
        'base_generation':created.data['generation'],'patch':{'kind':'exact','exact':'old','replacement':'new'}},
        key=key,subject=owner,expected=((rid,created.data['generation']),))
    assert patched.status == 'ok', wire(patched)
    ops = [packet(app,key,owner,'file.create',{**args,'name':'batch.txt'},'file-child-a'),
           packet(app,key,owner,'file.create',args,'file-child-b')]
    failed = await call(app,'file.batch',{'requests':ops},key=key,subject=owner)
    assert failed.status == 'error' and failed.error.code == 'batch_aborted', wire(failed)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE name='batch.txt'")[0] == 0
        assert tx.one("SELECT COUNT(*) FROM results WHERE request_id='file-child-a'")[0] == 0
    nested = packet(app,key,owner,'file.batch',{'requests':ops},'nested-file-batch')
    denied = await call(app,'file.batch',{'requests':[nested]},key=key,subject=owner)
    assert denied.status == 'error' and denied.error.code == 'operation_not_batchable', wire(denied)
    succeeded = await call(app,'file.batch',{'requests':ops[:1]},key=key,subject=owner,rid='file-batch-success')
    assert succeeded.status == 'ok' and succeeded.data['atomic'] is True, wire(succeeded)
    replay = await call(app,'file.batch',{'requests':ops[:1]},key=key,subject=owner,rid='file-batch-success')
    assert replay.status == 'ok' and replay.replayed, wire(replay)


@pytest.mark.asyncio
async def test_directory_owner_cannot_replace_another_authors_file(installed):
    app, _ = installed
    key, owner, _ = await register(app,'directory-owner')
    writer, author, _ = await register(app,'file-author')
    directory = await call(app,'file.mkdir',{'parent':'/main','name':'shared-files'},key=key,subject=owner)
    assert directory.status == 'ok', wire(directory)
    created = await call(app,'file.create',{'parent':directory.resources[0].id,
        'name':'author.txt','data':b64(b'author bytes')},key=writer,subject=author)
    assert created.status == 'ok', wire(created)
    rid = created.resources[0].id
    denied = await call(app,'file.write',{'id':rid,'base_revision':created.resources[0].revision,
        'data':b64(b'directory owner overwrite')},key=key,subject=owner,
        expected=((rid,created.data['generation']),))
    assert denied.status == 'error' and denied.error.code == 'permission_denied', wire(denied)


@pytest.mark.asyncio
async def test_binary_replacement_removes_old_search_projection(installed):
    app, _ = installed
    key, owner, _ = await register(app,'binary-replace')
    created = await call(app,'file.create',{'parent':'/@binary-replace/files','name':'document',
        'data':b64(b'old searchable secret'),'media_type':'text/plain'},key=key,subject=owner)
    rid = created.resources[0].id
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT text FROM projections WHERE resource_id=?',(rid,))[0] == 'old searchable secret'
    result = await call(app,'file.write',{'id':rid,'base_revision':created.resources[0].revision,
        'data':b64(b'\x00\xff'),'media_type':'application/octet-stream'},key=key,subject=owner,
        expected=((rid,created.data['generation']),))
    assert result.status == 'ok', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT text FROM projections WHERE resource_id=?',(rid,)) is None
