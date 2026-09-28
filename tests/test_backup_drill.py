"""A disposable PostgreSQL restore proves byte references and safe drill defaults."""
import hashlib
import asyncio
import threading
import zipfile
import httpx
import subprocess

import psycopg
import pytest

from msg.admin.backups import backup, restore
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import canonical, loads
from msg.core.errors import Failure
from msg.core.models import EffectJob,Principal
from msg.storage.git import LFSObjectStore
from msg.workers.effects import EffectWorker
from msg.workers.maintenance import run_maintenance
from msg.transports.http import create_app
from test_service import NOW,call, register


@pytest.mark.asyncio
async def test_pg_dump_and_file_copy_hold_writer_lock(installed,tmp_path,pg_dsn,monkeypatch):
    app,_=installed
    key,subject,_=await register(app,'backup-locked')
    original=app.metadata.backup
    started=threading.Event()
    release=threading.Event()

    def paused_dump(destination):
        started.set()
        assert release.wait(5)
        return original(destination)

    monkeypatch.setattr(app.metadata,'backup',paused_dump)
    archive=tmp_path/'locked.zip'
    saving=asyncio.create_task(backup(app,archive))
    try:
        assert await asyncio.to_thread(started.wait,5)
        writing=asyncio.create_task(call(app,'content.post_create',
            {'parent':'/main','body':'late writer'},key=key,subject=subject))
        await asyncio.sleep(0.1)
        assert not writing.done()
    finally:
        release.set()
    await saving
    result=await writing
    assert result.status=='ok'
    restore(archive,tmp_path/'lock-etc',tmp_path/'lock-data',postgres_dsn=pg_dsn)
    with psycopg.connect(pg_dsn) as connection:
        assert connection.execute("SELECT COUNT(*) FROM resources WHERE body LIKE '%late writer%'").fetchone()[0]==0


@pytest.mark.asyncio
async def test_restore_preserves_shared_lfs_hardlinks_and_disables_outbound(installed, tmp_path, pg_dsn, monkeypatch):
    app,_=installed
    key,subject,_=await register(app,'backup-lfs')
    created=await call(app,'git.create',{'parent':'/@backup-lfs','name':'source.git'},
                       key=key,subject=subject)
    assert created.status=='ok'
    rid=created.resources[0].id
    repo=app.settings.server.repositories_dir/(rid+'.git')
    data=b'backup-and-restore-lfs\n'*100
    oid=hashlib.sha256(data).hexdigest()
    staged=tmp_path/'staged-lfs'
    staged.write_bytes(data)
    LFSObjectStore(repo,app.settings.server.blob_dir).publish(oid,staged,len(data))
    assert (repo/'lfs'/'objects'/oid[:2]/oid[2:4]/oid).stat().st_ino == \
           (app.settings.server.blob_dir/oid).stat().st_ino
    pending=EffectJob(id='drill-pending-mail',event_id='drill-event',kind='mail',
        dedupe_key='drill-pending-mail',
        principal=Principal(actor=subject,subject=subject,credential_id=None,
                            method='local',certificates=(),ceiling=()),
        operation='mail.send',arguments={},state='pending',attempts=0,
        next_attempt_at=NOW,lease_until=None)
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(pending)
    archive=tmp_path/'backup.zip'
    await backup(app,archive)
    restored_config=tmp_path/'drill-etc'
    restored_data=tmp_path/'drill-data'
    result=restore(archive,restored_config,restored_data,postgres_dsn=pg_dsn)
    assert result['outbound']=='disabled_recovery_drill'
    assert result['revocation_replay']=='required' and result['promotion']=='blocked'
    settings=load_settings(restored_config)
    restored_repo=settings.server.repositories_dir/(rid+'.git')
    linked=restored_repo/'lfs'/'objects'/oid[:2]/oid[2:4]/oid
    shared=settings.server.blob_dir/oid
    assert linked.read_bytes()==data
    assert linked.stat().st_ino==shared.stat().st_ino
    assert shared.stat().st_nlink>=2
    marker=loads((restored_config/'recovery-drill.json').read_bytes())
    assert marker['outbound_enabled'] is False
    assert marker['source_backup_sha256']==hashlib.sha256(archive.read_bytes()).hexdigest()
    assert settings.server.valkey_url is None
    assert settings.server.mail is None
    assert not (restored_config/'root').exists()
    # A copied/deleted config marker must not resurrect snapshot authority.
    (restored_config/'recovery-drill.json').unlink()
    async def forbidden_sync(*args,**kwargs):
        raise AssertionError('loading a quarantine rewrote release resources')
    with monkeypatch.context() as patched:
        patched.setattr('msg.bootstrap.sync_system_sources',forbidden_sync)
        restored=Application(settings,clock=lambda:NOW)
        await restored.load()
    anonymous=await call(restored,'discovery.get',{'id':rid})
    assert anonymous.status=='error' and anonymous.error.code=='recovery_quarantined'
    authenticated=await call(restored,'discovery.get',{'id':rid},key=key,subject=subject)
    assert authenticated.status=='error' and authenticated.error.code=='recovery_quarantined'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(restored)),
                                 base_url='http://testserver') as http:
        health=await http.get('/healthz')
        assert health.status_code==503 and health.json()['status']=='recovery_quarantined'
        assert health.json()['ready'] is False and health.json()['outbound_enabled'] is False
        assert (await http.get('/main')).status_code==503
        assert (await http.head('/healthz')).content==b''
    assert await run_maintenance(restored,'deliver_due_todos',scheduled=True)=={
        'skipped':'recovery_quarantine'}
    rejected=await call(restored,'content.post_create',
                        {'parent':'/main','body':'should not publish'},
                        key=key,subject=subject)
    assert rejected.status=='error' and rejected.error.code=='writes_paused'
    denied_archive=await call(restored,'content.archive',{'id':rid},
                              key=key,subject=subject)
    assert denied_archive.status=='error' and denied_archive.error.code=='writes_paused'
    class ForbiddenSender:
        def __init__(self):self.calls=0
        async def send(self,*args,**kwargs):
            self.calls+=1
            raise AssertionError('recovery drill sent an external effect')
    sender=ForbiddenSender()
    worker=EffectWorker(restored,mail_sender=sender,webhook_sender=sender,
                        tool_runner=sender)
    assert await worker.run_once() is False
    assert await worker._claim()==(None,False)  # Database gate, independent of process cache.
    assert sender.calls==0
    async with restored.metadata.transaction(write=False) as tx:
        quarantine=tx.setting('recovery_quarantine')
        assert quarantine['authority']=='health_only'
        assert quarantine['revocation_replay']=='required'
        runtime=tx.setting('runtime_config')
        assert runtime['accept_writes'] is False
        assert runtime['cleanup_enabled'] is False
        assert await tx.job(pending.id)==pending
    await restored.close()


@pytest.mark.asyncio
async def test_restore_rejects_lfs_digest_conflict_before_touching_pg(installed,tmp_path,pg_dsn):
    app,_=installed
    key,subject,_=await register(app,'backup-corrupt')
    created=await call(app,'git.create',{'parent':'/@backup-corrupt','name':'source.git'},
                       key=key,subject=subject)
    assert created.status=='ok'
    rid=created.resources[0].id
    data=b'valid-object'
    oid=hashlib.sha256(data).hexdigest()
    staged=tmp_path/'staged-lfs';staged.write_bytes(data)
    LFSObjectStore(app.settings.server.repositories_dir/(rid+'.git'),
                   app.settings.server.blob_dir).publish(oid,staged,len(data))
    archive=tmp_path/'backup.zip'
    await backup(app,archive)
    altered=tmp_path/'altered.zip'
    with zipfile.ZipFile(archive) as original:
        files={name:original.read(name) for name in original.namelist()}
    target=f'blobs/{oid}'
    files[target]=b'invalid-object'
    manifest=loads(files['manifest.json'])
    manifest['files'][target]=hashlib.sha256(files[target]).hexdigest()
    files['manifest.json']=canonical(manifest)
    with zipfile.ZipFile(altered,'w') as output:
        for name,data in files.items():
            output.writestr(name,data)
    with pytest.raises(Failure,match='backup_lfs_digest_mismatch'):
        restore(altered,tmp_path/'bad-etc',tmp_path/'bad-data',postgres_dsn=pg_dsn)
    assert not (tmp_path/'bad-etc').exists()
    assert not (tmp_path/'bad-data').exists()


@pytest.mark.asyncio
async def test_interrupted_restore_rolls_back_dump_and_gate_together(installed,tmp_path,pg_dsn,monkeypatch):
    from msg.admin import restore_database
    app,_=installed
    archive=tmp_path/'atomic.zip'
    await backup(app,archive)
    original=restore_database.subprocess.run
    def fail_inside_import(argv,**kwargs):
        if argv[0]=='psql':
            from pathlib import Path
            script=Path(argv[argv.index('--file')+1])
            with script.open('a') as stream:
                stream.write('\nSELECT 1/0;\n')
        return original(argv,**kwargs)
    monkeypatch.setattr(restore_database.subprocess,'run',fail_inside_import)
    with pytest.raises(subprocess.CalledProcessError):
        restore(archive,tmp_path/'failed-etc',tmp_path/'failed-data',postgres_dsn=pg_dsn)
    with psycopg.connect(pg_dsn) as connection:
        assert connection.execute("SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=current_schema() LIMIT 1").fetchone() is None
    assert not (tmp_path/'failed-etc').exists() and not (tmp_path/'failed-data').exists()
