from pathlib import Path
import subprocess,sys,os,zipfile
import pytest
from msg.admin.backups import backup,restore
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import wire
from test_service import register,call,NOW


@pytest.mark.asyncio
async def test_consistent_backup_restores_content_but_never_root_private_key(installed,tmp_path,pg_dsn):
    app,_=installed
    key,uid,_=await register(app,'backup-agent')
    posted=await call(app,'content.post_create',{'parent':'/main','body':'survives recovery'},key=key,subject=uid)
    binary=await app.contents.put_bytes(b'\x00\xffbackup', 'application/octet-stream')
    result=await backup(app,tmp_path/'data-backup.zip')
    assert result['root_private_key_included'] is False
    with zipfile.ZipFile(tmp_path/'data-backup.zip') as z:
        assert not any(name.startswith('root/') or 'key.json' in name for name in z.namelist())
    restore(tmp_path/'data-backup.zip',tmp_path/'restored-etc',tmp_path/'restored-data',postgres_dsn=pg_dsn)
    restored=Application(load_settings(tmp_path/'restored-etc'),clock=lambda:NOW)
    await restored.load()
    read=await call(restored,'discovery.get',{'id':posted.resources[0].id})
    assert read.status=='ok' and read.data['content']=='survives recovery',wire(read)
    assert await restored.contents.read_bytes(binary)==b'\x00\xffbackup'
    assert not (tmp_path/'restored-etc'/'root').exists()
    await restored.close()


def test_unattended_init_never_generates_root_or_accepts_pin_flag(tmp_path):
    base=[sys.executable,'-m','msg.daemon','--config-dir',str(tmp_path/'etc'),'init','--data-dir',str(tmp_path/'data')]
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src')}
    result=subprocess.run(base,input='',text=True,capture_output=True,env=env)
    assert result.returncode==78
    assert 'interactive_pin_required' in result.stdout
    assert not (tmp_path/'etc').exists() and not (tmp_path/'data').exists()
    result=subprocess.run(base+['--pin','123456'],input='',text=True,capture_output=True,env=env)
    assert result.returncode==2
