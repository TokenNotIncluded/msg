import hashlib
import multiprocessing
import os
from pathlib import Path
import time

import pytest
from msg.core.errors import Failure
from msg.storage.git import LFSObjectStore


def oid(data):
    return hashlib.sha256(data).hexdigest()


def store(tmp_path, repo='repo.git'):
    return LFSObjectStore(tmp_path/repo,tmp_path/'shared')


def stage(tmp_path,data):
    path=tmp_path/('stage-'+oid(data))
    path.write_bytes(data)
    return path


def assert_failure(code,call):
    with pytest.raises(Failure) as exc:
        call()
    assert exc.value.code==code


def test_staging_digest_not_just_size(tmp_path):
    s=store(tmp_path)
    bad=stage(tmp_path,b'evil')
    assert_failure('lfs_digest_mismatch',lambda:s.publish(oid(b'good'),bad,4))
    assert not s.path(oid(b'good')).exists()
    assert not (tmp_path/'shared'/oid(b'good')).exists()


def test_existing_repo_corruption_is_not_accepted(tmp_path):
    s=store(tmp_path)
    good=b'good'
    path=s.path(oid(good));path.parent.mkdir(parents=True);path.write_bytes(b'evil')
    assert_failure('lfs_digest_mismatch',lambda:s.publish(oid(good),stage(tmp_path,good),4))
    assert path.read_bytes()==b'evil'  # Never silently replace an unexplained conflict.


def test_existing_canonical_corruption_is_not_linked(tmp_path):
    s=store(tmp_path)
    s.shared.mkdir();(s.shared/oid(b'good')).write_bytes(b'evil')
    assert_failure('lfs_digest_mismatch',lambda:s.publish(oid(b'good'),stage(tmp_path,b'good'),4))
    assert not s.path(oid(b'good')).exists()


def test_explicit_legacy_migration_is_atomic_and_idempotent(tmp_path):
    s=store(tmp_path)
    data=b'legacy bytes\n';key=oid(data)
    legacy=s.path(key);legacy.parent.mkdir(parents=True);legacy.write_bytes(data)
    before=legacy.stat().st_ino
    result=s.migrate_legacy(key,quota_bytes=len(data))
    assert result=={'state':'migrated','size':len(data)}
    canonical=s.shared/key
    assert legacy.read_bytes()==data
    assert legacy.stat().st_ino==canonical.stat().st_ino
    assert legacy.stat().st_ino!=before
    assert s.migrate_legacy(key,quota_bytes=len(data))=={'state':'already_shared','size':len(data)}


def test_publish_also_repairs_valid_legacy_layout(tmp_path):
    s=store(tmp_path);data=b'legacy';key=oid(data)
    legacy=s.path(key);legacy.parent.mkdir(parents=True);legacy.write_bytes(data)
    s.publish(key,stage(tmp_path,data),len(data))
    assert legacy.stat().st_ino==(s.shared/key).stat().st_ino


def test_migration_reuses_existing_cas_without_double_charge(tmp_path):
    data=b'one';key=oid(data)
    one=store(tmp_path,'a.git');two=store(tmp_path,'b.git')
    one.publish(key,stage(tmp_path,data),len(data),quota_bytes=len(data))
    path=two.path(key);path.parent.mkdir(parents=True);path.write_bytes(data)
    two.migrate_legacy(key,quota_bytes=len(data))
    assert two.shared_usage()==len(data)
    assert path.stat().st_ino==one.path(key).stat().st_ino


def test_migration_quota_failure_keeps_legacy_bytes(tmp_path):
    s=store(tmp_path);data=b'legacy';key=oid(data)
    path=s.path(key);path.parent.mkdir(parents=True);path.write_bytes(data)
    inode=path.stat().st_ino
    assert_failure('lfs_deployment_capacity_exceeded',lambda:s.migrate_legacy(key,quota_bytes=1))
    assert path.read_bytes()==data and path.stat().st_ino==inode
    assert not (s.shared/key).exists()


def test_migration_crash_before_replace_keeps_old_acl_root(tmp_path,monkeypatch):
    s=store(tmp_path);data=b'legacy';key=oid(data)
    path=s.path(key);path.parent.mkdir(parents=True);path.write_bytes(data)
    inode=path.stat().st_ino
    replace=os.replace
    def crash(source,destination):
        if Path(destination)==path:
            raise OSError('injected rename failure')
        return replace(source,destination)
    monkeypatch.setattr(os,'replace',crash)
    with pytest.raises(OSError):s.migrate_legacy(key)
    assert path.read_bytes()==data and path.stat().st_ino==inode
    assert not list(path.parent.glob('.pending-*'))
    monkeypatch.setattr(os,'replace',replace)
    s.migrate_legacy(key)
    assert path.stat().st_ino==(s.shared/key).stat().st_ino


@pytest.mark.parametrize('where', ['stage','canonical','repository'])
def test_symlink_bytes_are_never_published(tmp_path,where):
    s=store(tmp_path);data=b'safe';key=oid(data)
    staged=stage(tmp_path,data)
    if where=='stage':
        linked=tmp_path/'symlink-stage';linked.symlink_to(staged);staged=linked
    else:
        target=s.path(key) if where=='repository' else s.shared/key
        target.parent.mkdir(parents=True,exist_ok=True);target.symlink_to(staged)
    assert_failure('lfs_object_conflict',lambda:s.publish(key,staged,len(data)))


def test_pure_stat_does_not_create_lock_or_migrate(tmp_path):
    s=store(tmp_path);data=b'legacy';key=oid(data)
    path=s.path(key);path.parent.mkdir(parents=True);path.write_bytes(data)
    before=sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    assert s.size(key)==len(data)
    assert s.path(key).read_bytes()==data
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))==before
    assert not s.shared.exists()


def test_gc_retains_each_migrated_repository_link(tmp_path):
    s=store(tmp_path);data=b'legacy';key=oid(data)
    path=s.path(key);path.parent.mkdir(parents=True);path.write_bytes(data)
    s.migrate_legacy(key)
    index=tmp_path/'index';index.mkdir()
    assert s.collect_unlinked(older_than=time.time()+1,index=index)==(0,0)
    path.unlink()
    assert s.collect_unlinked(older_than=time.time()+1,index=index)==(1,len(data))


def _parallel_publish(root,index,start,results):
    root=Path(root);data=(str(index)*4096).encode();s=store(root,f'{index}.git')
    staged=stage(root,data)
    usage=s.shared_usage
    def delayed_usage():
        result=usage();time.sleep(0.2);return result
    s.shared_usage=delayed_usage
    results.put('ready')
    start.wait()
    try:
        s.publish(oid(data),staged,len(data),quota_bytes=4096)
        results.put('ok')
    except Failure as exc:
        results.put(exc.code)


def test_capacity_check_is_serialized_between_real_processes(tmp_path):
    ctx=multiprocessing.get_context('spawn')
    start=ctx.Event();results=ctx.Queue()
    workers=[ctx.Process(target=_parallel_publish,args=(str(tmp_path),i,start,results)) for i in (1,2)]
    for worker in workers:worker.start()
    try:
        assert [results.get(timeout=15) for _ in workers]==['ready','ready']
        start.set()
        values=[results.get(timeout=15) for _ in workers]
        assert sorted(values)==['lfs_deployment_capacity_exceeded','ok']
        assert store(tmp_path).shared_usage()==4096
    finally:
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():worker.terminate();worker.join()
        results.close()


def test_storage_lock_has_bounded_retryable_wait(tmp_path):
    first=store(tmp_path);second=store(tmp_path)
    with first._locked():
        start=time.monotonic()
        with pytest.raises(Failure) as exc:
            with second._locked(timeout=0.04):
                pytest.fail('second writer must not acquire the held lock')
        assert exc.value.code=='server_busy' and exc.value.retryable
        assert time.monotonic()-start<1


def test_repository_storage_symlink_is_refused(tmp_path):
    s=store(tmp_path);data=b'bytes'
    outside=tmp_path/'outside';outside.mkdir()
    s.root.parent.mkdir(parents=True)
    s.root.symlink_to(outside,target_is_directory=True)
    assert_failure('lfs_object_conflict',lambda:s.publish(oid(data),stage(tmp_path,data),len(data)))
    assert not list(outside.iterdir())
