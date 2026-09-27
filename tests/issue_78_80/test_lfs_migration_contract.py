from dataclasses import replace
import hashlib
import pytest
from msg.core.errors import Failure
from msg.extensions.repositories import NativeGitStore


async def test_migrate_is_explicit_write_with_current_authorization(harness):
    h=harness
    repo=await h.create('r_repo',type='repo')
    native=NativeGitStore(h.app)
    lfs=native.lfs(repo.id)
    data=b'legacy complete object'
    oid=hashlib.sha256(data).hexdigest()
    path=lfs.path(oid);path.parent.mkdir(parents=True);path.write_bytes(data)
    before_inode=path.stat().st_ino
    read=await h.invoke('git.lfs_read',{'id':repo.id,'oid':oid})
    assert read.data['resource_id']==repo.id
    assert path.stat().st_ino==before_inode
    assert not (lfs.shared/oid).exists()
    migrated=await h.invoke('git.lfs_migrate',{'id':repo.id,'oid':oid})
    assert migrated.data['state']=='migrated'
    assert path.stat().st_ino==(lfs.shared/oid).stat().st_ino
    assert h.app.registry.operation('git.lfs_migrate').effect=='transaction'
    repeat=await h.invoke('git.lfs_migrate',{'id':repo.id,'oid':oid})
    assert repeat.data['state']=='already_shared'
    async with h.app.metadata.transaction(write=True) as tx:
        await tx.replace(replace(repo,mode=0,owner='u_other',generation=repo.generation+1),repo.generation)
    with pytest.raises(Failure) as exc:
        await h.invoke('git.lfs_migrate',{'id':repo.id,'oid':oid})
    assert exc.value.code=='permission_denied'


async def test_existing_credential_ceiling_does_not_gain_migration(harness):
    h=harness
    repo=await h.create('r_repo',type='repo')
    old=replace(h.ctx.principal,ceiling=tuple(replace(grant,
        operations=grant.operations-{'git.lfs_migrate@1'}) for grant in h.ctx.principal.ceiling))
    with pytest.raises(Failure) as exc:
        await h.invoke('git.lfs_migrate',{'id':repo.id,'oid':'0'*64},context=replace(h.ctx,principal=old))
    assert exc.value.code=='credential_ceiling'
    assert not h.app.settings.server.repositories_dir.exists()


async def test_lfs_migration_cannot_target_an_unrelated_resource_type(harness):
    h=harness
    file=await h.create('r_file',type='file')
    with pytest.raises(Failure) as exc:
        await h.invoke('git.lfs_migrate',{'id':file.id,'oid':'0'*64})
    assert exc.value.code=='not_a_repository'
