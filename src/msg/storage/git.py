"""Private Git text storage and streamed binary CAS, never a public Git endpoint."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from msg.core.codec import canonical, digest, loads
from msg.core.errors import Failure, require
from msg.core.models import BlobRef


def durable_write(path: Path, data: bytes, mode: int = 0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.pending-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class GitContentStore:
    def __init__(self,path: Path, *, binary_dir: Path | None = None,
                 staging_dir: Path | None = None):
        self.path=Path(path)
        self.repo=self.path/'private.git'
        self.index=self.path/'index'
        self.binary=Path(binary_dir) if binary_dir is not None else self.path/'binary'
        self.staging=Path(staging_dir) if staging_dir is not None else self.path/'staging'
        for p in (self.path,self.index,self.binary,self.staging):
            p.mkdir(parents=True,exist_ok=True)
        if not self.repo.exists():
            subprocess.run(['git','init','--bare',str(self.repo)],check=True,capture_output=True)
        self.env={"PATH":os.environ.get('PATH','/usr/bin:/bin'),"LANG":"C.UTF-8",
                  "HOME":str(self.path),"GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null",
                  "GIT_AUTHOR_NAME":"msg","GIT_AUTHOR_EMAIL":"msg@localhost",
                  "GIT_COMMITTER_NAME":"msg","GIT_COMMITTER_EMAIL":"msg@localhost"}

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
                    os.replace(name,destination)
                    directory=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                entry={'kind':'binary','size':size}
            durable_write(self.index/self._key(ref),canonical(entry))
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

    async def pin(self,blob,lease_id):
        entry=self._entry(blob)
        name=hashlib.sha256(lease_id.encode()).hexdigest()
        if entry['kind']=='git':
            await asyncio.to_thread(self._run,'update-ref',f'refs/pins/{name}/{self._key(blob)}',entry['oid'])
        else:
            durable_write(self.path/'pins'/name/self._key(blob),b'1\n')

    async def unpin(self,blob,lease_id):
        entry=self._entry(blob)
        name=hashlib.sha256(lease_id.encode()).hexdigest()
        if entry['kind']=='git':
            await asyncio.to_thread(self._run,'update-ref','-d',f'refs/pins/{name}/{self._key(blob)}')
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
