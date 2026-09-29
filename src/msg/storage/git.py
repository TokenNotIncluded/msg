"""Private Git text storage and streamed binary CAS, never a public Git endpoint."""
from __future__ import annotations

import asyncio
import hashlib
import fcntl
import stat
import time
from contextlib import contextmanager
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from msg.core.codec import canonical, digest, loads
from msg.core.errors import Failure, require
from msg.core.models import BlobRef
from msg.atomic_file import durable_write


class LFSObjectStore:
    """Verified shared bytes; repository hardlinks remain the only ACL roots.

    Publishers still hold the metadata transaction for current authorization.
    A shared filesystem lock also serializes LFS capacity, migration and GC
    across worker processes. It is not a substitute for PostgreSQL authority.
    """
    def __init__(self, repo: Path, shared_root: Path | None = None):
        self.root=Path(repo)/'lfs'/'objects'
        self.shared=Path(shared_root) if shared_root is not None else None

    def path(self, oid: str) -> Path:
        require(isinstance(oid,str) and re.fullmatch(r'[0-9a-f]{64}',oid) is not None,
                'invalid_lfs_oid')
        return self.root/oid[:2]/oid[2:4]/oid

    def size(self, oid: str) -> int | None:
        path=self.path(oid)
        require(not path.is_symlink(),'lfs_object_conflict')
        return path.stat().st_size if path.is_file() else None

    def _shared_path(self,oid: str) -> Path:
        require(self.shared is not None,'lfs_shared_store_required')
        self.path(oid)
        return self.shared/oid

    @contextmanager
    def _locked(self, *, timeout=30.0):
        require(self.shared is not None,'lfs_shared_store_required')
        require(not self.shared.is_symlink(),'lfs_shared_volume_required')
        self.shared.mkdir(parents=True,exist_ok=True)
        try:
            fd=os.open(self.shared/'.msg-lfs.lock',
                       os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        except OSError as exc:
            raise Failure('lfs_object_conflict') from exc
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode),'lfs_object_conflict')
            deadline=time.monotonic()+timeout
            while True:
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic()<deadline,'server_busy',retryable=True)
                    time.sleep(min(0.01,max(0,deadline-time.monotonic())))
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _sync(directory):
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

    @staticmethod
    def _verify(path,oid,size):
        """Do not follow links or accept same-size corruption/replaced bytes."""
        require(type(size) is int and 0<=size<=2**63-1,'lfs_size_mismatch')
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        except FileNotFoundError as exc:
            raise Failure('lfs_object_missing') from exc
        except OSError as exc:
            raise Failure('lfs_object_conflict') from exc
        with os.fdopen(fd,'rb') as source:
            before=os.fstat(source.fileno())
            require(stat.S_ISREG(before.st_mode),'lfs_object_conflict')
            require(before.st_size==size,'lfs_size_mismatch')
            hasher=hashlib.sha256()
            actual=0
            while chunk:=source.read(1024*1024):
                actual+=len(chunk)
                require(actual<=size,'lfs_size_mismatch')
                hasher.update(chunk)
            after=os.fstat(source.fileno())
        require(actual==size,'lfs_size_mismatch')
        require(hasher.hexdigest()==oid,'lfs_digest_mismatch')
        require((before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==
                (after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),
                'lfs_object_conflict')
        return after

    def shared_usage(self) -> int:
        require(self.shared is not None,'lfs_shared_store_required')
        if not self.shared.exists():return 0
        total=0
        for path in self.shared.iterdir():
            if not re.fullmatch(r'[0-9a-f]{64}',path.name):continue
            info=path.lstat()
            require(stat.S_ISREG(info.st_mode),'lfs_object_conflict')
            total+=info.st_size
        return total

    def _publish(self,oid,staged,size,quota_bytes):
        destination=self.path(oid)
        shared=self._shared_path(oid)
        if quota_bytes is not None:
            require(type(quota_bytes) is int and quota_bytes>=0,'invalid_lfs_deployment_capacity')
        require(not any(path.is_symlink() for path in
            (self.root,self.root.parent,destination.parent.parent,destination.parent)),
            'lfs_object_conflict')
        self._verify(staged,oid,size)
        if destination.exists() or destination.is_symlink():
            self._verify(destination,oid,size)
        destination.parent.mkdir(parents=True,exist_ok=True)
        require(shared.parent.stat().st_dev==destination.parent.stat().st_dev,
                'lfs_shared_volume_required')
        if shared.exists() or shared.is_symlink():
            self._verify(shared,oid,size)
        else:
            if quota_bytes is not None:
                require(self.shared_usage()+size<=quota_bytes,'lfs_deployment_capacity_exceeded')
            fd,temporary=tempfile.mkstemp(prefix='.pending-',dir=shared.parent)
            try:
                with os.fdopen(fd,'wb') as target,staged.open('rb') as source:
                    hasher=hashlib.sha256()
                    actual=0
                    while chunk:=source.read(1024*1024):
                        actual+=len(chunk)
                        require(actual<=size,'lfs_size_mismatch')
                        hasher.update(chunk);target.write(chunk)
                    require(actual==size,'lfs_size_mismatch')
                    require(hasher.hexdigest()==oid,'lfs_digest_mismatch')
                    target.flush();os.fsync(target.fileno())
                # Never overwrite an existing canonical inode: other repositories
                # can already hold hardlinks to it. A collision is reverified.
                try:os.link(temporary,shared)
                except FileExistsError:self._verify(shared,oid,size)
                self._sync(shared.parent)
            finally:
                os.unlink(temporary)
        if destination.exists() and os.path.samefile(shared,destination):
            return False
        # An atomic link replacement retains a legacy ACL root through every
        # crash point. No published path ever points at a partly copied file.
        with tempfile.TemporaryDirectory(prefix='.pending-',dir=destination.parent) as temp:
            linked=Path(temp)/'object'
            os.link(shared,linked)
            os.replace(linked,destination)
            self._sync(destination.parent)
        self._sync(destination.parent)
        return True

    def publish(self, oid: str, staged: Path, size: int, *, quota_bytes: int | None = None):
        self.path(oid)
        with self._locked():
            self._publish(oid,Path(staged),size,quota_bytes)

    def migrate_legacy(self,oid: str, *, quota_bytes: int | None = None):
        """Explicit authenticated write, never called by HEAD/GET/stat/batch."""
        source=self.path(oid)
        with self._locked():
            require(source.is_file() and not source.is_symlink(),'lfs_object_missing')
            size=source.stat().st_size
            changed=self._publish(oid,source,size,quota_bytes)
        return {'state':'migrated' if changed else 'already_shared','size':size}

    def collect_unlinked(self, *, older_than: float, index: Path) -> tuple[int,int]:
        """Collect only after all repository links and content references vanish."""
        require(self.shared is not None,'lfs_shared_store_required')
        if not self.shared.exists():return (0,0)
        removed=bytes_removed=0
        with self._locked():
            for path in self.shared.iterdir():
                if not re.fullmatch(r'[0-9a-f]{64}',path.name):continue
                info=path.lstat()
                require(stat.S_ISREG(info.st_mode),'lfs_object_conflict')
                if info.st_nlink!=1 or info.st_mtime>older_than or (index/path.name).exists():
                    continue
                path.unlink()
                removed+=1;bytes_removed+=info.st_size
            if removed:self._sync(self.shared)
        return removed,bytes_removed


class GitContentStore:
    def __init__(self,path: Path, *, binary_dir: Path | None = None,
                 staging_dir: Path | None = None, group_read: bool = False):
        require(type(group_read) is bool,'invalid_content_group_read')
        self.group_read=group_read
        self.path=Path(path)
        self.repo=self.path/'private.git'
        self.index=self.path/'index'
        self.binary=Path(binary_dir) if binary_dir is not None else self.path/'binary'
        self.staging=Path(staging_dir) if staging_dir is not None else self.path/'staging'
        self.env={"PATH":os.environ.get('PATH','/usr/bin:/bin'),"LANG":"C.UTF-8",
                  "HOME":str(self.path),"GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null",
                  "GIT_AUTHOR_NAME":"msg","GIT_AUTHOR_EMAIL":"msg@localhost",
                  "GIT_COMMITTER_NAME":"msg","GIT_COMMITTER_EMAIL":"msg@localhost"}
        # An existing installation needs an explicit offline permission review.
        # Validate before creating/chmod'ing anything, including staging paths.
        if group_read:
            for p in (self.path,self.index,self.binary):
                if p.exists() or p.is_symlink():
                    require(p.is_dir() and not p.is_symlink() and
                            p.stat().st_mode & 0o2077 == 0o2050,
                            'content_sharing_not_prepared')
            if self.repo.exists() or self.repo.is_symlink():
                require(self.repo.is_dir() and not self.repo.is_symlink(),
                        'content_sharing_not_prepared')
                config=subprocess.run(['git','--git-dir',str(self.repo),'config',
                    '--local','core.sharedRepository'],capture_output=True,env=self.env,timeout=30)
                require(config.returncode==0 and config.stdout.strip()==b'0640',
                        'content_sharing_not_prepared')
        for p in (self.path,self.index,self.binary):
            existed=p.exists()
            p.mkdir(parents=True,exist_ok=True)
            if group_read and not existed:
                p.chmod(0o2750)
        self.staging.mkdir(parents=True,exist_ok=True)
        if not self.repo.exists():
            shared=['--shared=0640'] if group_read else []
            subprocess.run(['git','init','--bare',*shared,str(self.repo)],
                           check=True,capture_output=True,env=self.env,timeout=30)

    def _run(self,*args,input=None,stdin=None):
        result=subprocess.run(['git','--git-dir',str(self.repo),'-c','core.fsync=all',
                               '-c','core.logAllRefUpdates=false',*args],
                              input=input,stdin=stdin,capture_output=True,env=self.env,timeout=120)
        if result.returncode:
            raise Failure('content_store_error')
        return result.stdout.strip().decode('ascii')

    @staticmethod
    def _key(ref):
        value=ref.digest if isinstance(ref,BlobRef) else ref
        require(type(value) is str and re.fullmatch(r'sha256:[0-9a-f]{64}',value) is not None,'invalid_digest')
        return value[7:]

    def _entry(self,ref):
        path=self.index/self._key(ref)
        require(path.is_file(),'content_missing')
        return loads(path.read_bytes())

    async def put(self,chunks,media_type,expected_digest=None):
        hasher=hashlib.sha256()
        size=0
        fd,name=tempfile.mkstemp(dir=self.staging)
        try:
            with os.fdopen(fd,'wb') as stream:
                async for chunk in chunks:
                    require(isinstance(chunk,bytes),'invalid_chunk')
                    hasher.update(chunk)
                    size+=len(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            hashed='sha256:'+hasher.hexdigest()
            if expected_digest is not None:
                require(expected_digest==hashed,'digest_mismatch')
            ref=BlobRef(digest=hashed,size=size,media_type=media_type)
            is_text=media_type.startswith('text/') or media_type in {'application/json','application/msg-template'}
            if is_text:
                def save_git():
                    with open(name,'rb') as source:
                        oid=self._run('hash-object','-w','--stdin',stdin=source)
                    self._run('update-ref','refs/staging/'+self._key(ref),oid)
                    return oid
                oid=await asyncio.to_thread(save_git)
                entry={'kind':'git','oid':oid,'size':size}
            else:
                destination=self.binary/self._key(ref)
                if not destination.exists():
                    if self.group_read:
                        # Rename retains the staging GID, so explicitly use the
                        # prepared destination group. Staging itself stays private.
                        os.chown(name,-1,self.binary.stat().st_gid)
                        os.chmod(name,0o640)
                        with open(name,'rb') as staged:
                            os.fsync(staged.fileno())
                    os.replace(name,destination)
                    directory=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                entry={'kind':'binary','size':size}
            durable_write(self.index/self._key(ref),canonical(entry),
                          mode=0o640 if self.group_read else 0o600)
            return ref
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def put_bytes(self,data,media_type='text/markdown'):
        async def parts():
            yield data
        return await self.put(parts(),media_type)

    async def read(self,blob,byte_range=None):
        entry=self._entry(blob)
        start,end=byte_range if byte_range is not None else (0,blob.size)
        require(type(start) is int and type(end) is int and 0<=start<=end<=blob.size,'invalid_byte_range')
        require(entry['size']==blob.size,'content_size_mismatch')
        if entry['kind']=='binary':
            with (self.binary/self._key(blob)).open('rb') as stream:
                stream.seek(start)
                remaining=end-start
                while remaining:
                    data=stream.read(min(65536,remaining))
                    require(bool(data),'content_truncated')
                    remaining-=len(data)
                    yield data
            return
        process=await asyncio.create_subprocess_exec('git','--git-dir',str(self.repo),'cat-file','blob',entry['oid'],
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=self.env)
        position=0
        try:
            while data:=await process.stdout.read(65536):
                stop=position+len(data)
                if position<end and stop>start:
                    yield data[max(0,start-position):min(len(data),end-position)]
                position=stop
            require(await process.wait()==0 and position==blob.size,'content_truncated')
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def read_bytes(self,blob, *, limit=1024*1024):
        require(blob.size<=limit,'use_transfer')
        return b''.join([chunk async for chunk in self.read(blob)])

    async def _update_pin_ref(self, *args):
        # Cancelling to_thread does not stop Git. Finish the mutation before the
        # metadata transaction can compensate it or release its writer fence.
        pending = asyncio.create_task(asyncio.to_thread(self._run, *args))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError as cancelled:
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    pass
                except BaseException:
                    break
            try:
                pending.result()
            except BaseException as exc:
                cancelled.add_note('pin update failed: ' + type(exc).__name__)
            raise

    async def pin(self,blob,lease_id):
        entry=self._entry(blob)
        name=hashlib.sha256(lease_id.encode()).hexdigest()
        if entry['kind']=='git':
            await self._update_pin_ref('update-ref',f'refs/pins/{name}/{self._key(blob)}',entry['oid'])
        else:
            durable_write(self.path/'pins'/name/self._key(blob),b'1\n')

    async def unpin(self,blob,lease_id):
        entry=self._entry(blob)
        name=hashlib.sha256(lease_id.encode()).hexdigest()
        if entry['kind']=='git':
            await self._update_pin_ref('update-ref','-d',f'refs/pins/{name}/{self._key(blob)}')
        else:
            (self.path/'pins'/name/self._key(blob)).unlink(missing_ok=True)

    async def pinned(self,blob,lease_id):
        entry=self._entry(blob)
        name=hashlib.sha256(lease_id.encode()).hexdigest()
        if entry['kind']=='binary':
            return (self.path/'pins'/name/self._key(blob)).exists()
        try:
            return bool(await asyncio.to_thread(self._run,'rev-parse','--verify',f'refs/pins/{name}/{self._key(blob)}'))
        except Failure:
            return False

    async def commit_revision(self,topic_id,revision):
        """Create and protect one resource revision before the PostgreSQL pointer commits."""
        entry=self._entry(revision.content)
        def commit():
            manifest=self._run('hash-object','-w','--stdin',input=canonical(revision))
            lines=f'100644 blob {manifest}\tmanifest.json\n'
            if entry['kind']=='git':
                lines+=f"100644 blob {entry['oid']}\tcontent\n"
            tree=self._run('mktree',input=lines.encode())
            parents=[]
            for parent in revision.parents:
                parent_path=self.path/'revisions'/parent
                require(parent_path.is_file(),'revision_content_missing')
                parents+=['-p',parent_path.read_text().strip()]
            oid=self._run('commit-tree',tree,*parents,input=(revision.id+'\n').encode())
            # IDs are generated internally, but hash all ref components anyway.
            topic=hashlib.sha256(topic_id.encode()).hexdigest()
            resource=hashlib.sha256(revision.resource_id.encode()).hexdigest()
            rev=hashlib.sha256(revision.id.encode()).hexdigest()
            self._run('update-ref',f'refs/topics/{topic}/{resource}/{rev}',oid)
            durable_write(self.path/'revisions'/revision.id,(oid+'\n').encode())
            return oid
        return await asyncio.to_thread(commit)
