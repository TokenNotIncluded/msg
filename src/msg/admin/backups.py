"""Explicit local data backup; encrypted root material is a separate operation."""
from __future__ import annotations
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
from msg.core.codec import canonical,loads
from msg.core.errors import Failure,require
from msg.config import load_settings,write_example


def _hash(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        while chunk:=source.read(65536):value.update(chunk)
    return value.hexdigest()


async def backup(app,destination):
    destination=Path(destination)
    require(not destination.exists() and not destination.is_symlink(),'backup_destination_exists')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='msg-backup-',dir=destination.parent) as tmp:
        directory=Path(tmp)
        async with app.metadata.transaction(write=True) as tx:
            # Holding the write reservation makes the DB snapshot and the referenced
            # content trees consistent. Git/CAS writes always precede SQL pointers.
            await asyncio.to_thread(app.metadata.backup,directory/'metadata.sqlite3')
            await asyncio.to_thread(shutil.copytree,app.settings.server.content_dir,directory/'content')
            native=app.settings.server.content_dir.parent/'repositories'
            if native.exists():await asyncio.to_thread(shutil.copytree,native,directory/'repositories')
            await asyncio.to_thread(shutil.copytree,app.settings.service_keys,directory/'service')
            shutil.copy2(app.settings.trust_file,directory/'root-public.json')
            # Credentials from mail.toml are deliberately not embedded. Restore
            # reenables SMTP only after the operator supplies its own configuration.
        files={str(path.relative_to(directory)):_hash(path) for path in directory.rglob('*') if path.is_file()}
        manifest={'format':'msg-data-backup-v1','service_url':app.settings.service_url,'files':files,
                  'root_private_key_included':False,'mail_credentials_included':False}
        (directory/'manifest.json').write_bytes(canonical(manifest))
        partial=directory/'archive.zip'
        with zipfile.ZipFile(partial,'x',compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ('manifest.json',*sorted(files)):
                archive.write(directory/name,name)
        os.chmod(partial,0o600)
        with partial.open('rb') as stream:os.fsync(stream.fileno())
        os.link(partial,destination)
    return {'status':'backup_created','path':str(destination),'sha256':_hash(destination),
            'root_private_key_included':False}


def restore(source,config_dir,data_dir):
    """Restore only into new locations; never overwrite a live installation."""
    config_dir,data_dir=Path(config_dir),Path(data_dir)
    require(not config_dir.exists() and not data_dir.exists(),'restore_target_exists')
    config_dir.parent.mkdir(parents=True,exist_ok=True)
    data_dir.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='msg-restore-',dir=data_dir.parent) as temp:
        directory=Path(temp)
        with zipfile.ZipFile(source) as archive:
            names=archive.namelist()
            require(len(names)==len(set(names)) and 'manifest.json' in names,'invalid_backup')
            manifest=loads(archive.read('manifest.json'))
            require(manifest.get('format')=='msg-data-backup-v1' and manifest.get('root_private_key_included') is False,'invalid_backup')
            require(set(names)==set(manifest['files'])|{'manifest.json'},'backup_file_mismatch')
            for info in archive.infolist():
                name=info.filename
                require(not name.startswith('/') and '\\' not in name and
                        all(part not in {'','..','.'} for part in name.split('/')),'unsafe_backup_path')
                require((info.external_attr>>16)&0o170000 not in {0o120000,0o060000,0o020000},'unsafe_backup_entry')
                target=directory/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                with archive.open(info) as incoming,target.open('xb') as output:
                    shutil.copyfileobj(incoming,output,length=65536)
                os.chmod(target,0o600)
                if name!='manifest.json':require(_hash(target)==manifest['files'][name],'backup_digest_mismatch')
        connection=sqlite3.connect((directory/'metadata.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
        try:require(connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok','backup_database_corrupt')
        finally:connection.close()
        data_dir.mkdir(mode=0o700)
        for name in ('metadata.sqlite3','content','repositories'):
            if (directory/name).exists():shutil.move(str(directory/name),data_dir/name)
        settings=write_example(config_dir,data_dir,manifest['service_url'])
        settings.service_keys.parent.mkdir(parents=True,exist_ok=True)
        shutil.move(str(directory/'service'),settings.service_keys)
        os.chmod(settings.service_keys,0o700)
        settings.trust_file.parent.mkdir(parents=True,exist_ok=True)
        shutil.move(str(directory/'root-public.json'),settings.trust_file)
        os.chmod(settings.trust_file,0o444)
    return {'status':'restored','root_admin_material':'restore_separate_encrypted_root_backup_locally',
            'mail':'disabled_until_configured'}
