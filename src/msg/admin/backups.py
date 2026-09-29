"""Explicit local data backup; encrypted root material is a separate operation."""
from __future__ import annotations
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
import zipfile
import psycopg
from psycopg.conninfo import conninfo_to_dict,make_conninfo
import subprocess
from msg.core.codec import canonical,loads
from msg.core.errors import Failure,require
from msg.config import load_settings,write_example
from msg.admin.restore_database import restore_dump
from msg.storage.git import durable_write

_FORMAT = 'msg-data-backup-v4'
_KEYS = ('online.key', 'receipt.key', 'tokens.key')


def _regular_tree(path):
    """Refuse links before copying secrets or bytes into an archive."""
    require(path.is_dir() and not path.is_symlink(), 'backup_source_missing')
    for root, directories, files in os.walk(path, followlinks=False):
        for name in (*directories, *files):
            entry=Path(root)/name
            require(not entry.is_symlink(), 'backup_symlink_forbidden')
            require(entry.is_dir() or entry.is_file(), 'backup_special_file_forbidden')


def _db_refs(connection):
    """Revision and transfer chunk SQL pointers into Git/CAS."""
    contents={}
    revisions=[]
    for revision_id, raw in connection.execute('SELECT id,body FROM revisions ORDER BY id'):
        body=loads(raw)
        content=body['content']
        require(content['digest'].startswith('sha256:'), 'backup_reference_invalid')
        contents[content['digest'][7:]]=content['size']
        revisions.append(revision_id)
    for (raw,) in connection.execute('SELECT body FROM chunks ORDER BY transfer_id,"offset"'):
        content=loads(raw)['content']
        require(content['digest'].startswith('sha256:'), 'backup_reference_invalid')
        contents[content['digest'][7:]]=content['size']
    from msg.market.references import content_references
    for content in content_references(connection.execute):
        require(content['digest'].startswith('sha256:'), 'backup_reference_invalid')
        contents[content['digest'][7:]]=content['size']
    live={row[0] for row in connection.execute('SELECT revision FROM resources WHERE revision IS NOT NULL')}
    require(live.issubset(set(revisions)), 'backup_revision_missing')
    return {'contents':contents, 'revisions':revisions}


def _verify_storage(directory, refs, *, settings=None, repair_git_layout=True):
    """Verify archived bytes, Git objects and the SQL-referenced content index."""
    content=(settings.server.content_dir if settings is not None else directory/'content')
    blobs=(settings.server.blob_dir if settings is not None else directory/'blobs')
    repos=(settings.server.repositories_dir if settings is not None else directory/'repositories')
    require(content.is_dir() and (content/'private.git').is_dir() and
            (content/'index').is_dir() and blobs.is_dir(),
            'backup_storage_missing')
    index_files={path.name:path for path in (content/'index').iterdir() if path.is_file()}
    require(set(refs['contents']).issubset(index_files), 'backup_content_missing')
    for key,index in index_files.items():
        entry=loads(index.read_bytes())
        size=refs['contents'].get(key,entry.get('size'))
        require(len(key)==64 and all(c in '0123456789abcdef' for c in key) and
                type(size) is int and size>=0, 'backup_reference_invalid')
        require(entry.get('size')==size, 'backup_content_size_mismatch')
        if entry.get('kind')=='binary':
            target=blobs/key
            require(target.is_file() and target.stat().st_size==size and
                    _hash(target)==key, 'backup_content_digest_mismatch')
        else:
            require(entry.get('kind')=='git', 'backup_content_kind_invalid')
            oid=entry.get('oid')
            require(type(oid) is str and len(oid) in (40,64) and
                    all(c in '0123456789abcdef' for c in oid), 'backup_git_oid_invalid')
            status,actual_size,actual_hash=_git_blob_digest(content/'private.git',oid)
            require(status==0 and actual_size==size and actual_hash==key,
                    'backup_git_content_mismatch')
    for revision in refs['revisions']:
        require(type(revision) is str and '/' not in revision and revision not in ('.','..'),
                'backup_revision_invalid')
        pointer=content/'revisions'/revision
        require(pointer.is_file(), 'backup_revision_content_missing')
        oid=pointer.read_text().strip()
        require(len(oid) in (40,64) and all(c in '0123456789abcdef' for c in oid),
                'backup_revision_oid_invalid')
        process=subprocess.run(['git','--git-dir',str(content/'private.git'),
                                'cat-file','-e',oid+'^{commit}'],capture_output=True,check=False)
        require(process.returncode==0, 'backup_revision_content_missing')
    for repository in (repos.iterdir() if repos.exists() else ()):
        require(repository.is_dir() and repository.name.endswith('.git'),
                'backup_repository_invalid')
        # ZIP stores files only. An empty bare repository still needs these
        # structural directories; recreating them changes no Git object/ref.
        for name in ('objects/info','objects/pack','refs/heads','refs/tags'):
            if repair_git_layout:
                (repository/name).mkdir(parents=True,exist_ok=True)
            else:
                require((repository/name).is_dir(), 'backup_git_layout_missing')
        process=subprocess.run(['git','--git-dir',str(repository),'fsck','--full','--no-reflogs'],
                               capture_output=True,check=False)
        require(process.returncode==0, 'backup_git_repository_invalid')
        objects=repository/'lfs'/'objects'
        if not objects.exists():
            continue
        for path in objects.rglob('*'):
            if not path.is_file():
                continue
            oid=path.name
            require(len(oid)==64 and all(c in '0123456789abcdef' for c in oid) and
                    path.relative_to(objects).parts==(oid[:2],oid[2:4],oid),
                    'backup_lfs_path_invalid')
            canonical_blob=blobs/oid
            require(canonical_blob.is_file() and _hash(path)==oid and
                    path.stat().st_size==canonical_blob.stat().st_size and
                    _hash(canonical_blob)==oid, 'backup_lfs_digest_mismatch')


def _relink_lfs(data_dir, settings):
    """ZIP cannot retain hardlinks; restore canonical shared-CAS link counts."""
    repositories=settings.server.repositories_dir
    if not repositories.exists():
        return
    for repository in repositories.iterdir():
        objects=repository/'lfs'/'objects'
        if not objects.exists():
            continue
        for path in objects.rglob('*'):
            if not path.is_file():
                continue
            shared=settings.server.blob_dir/path.name
            require(path.stat().st_dev==shared.stat().st_dev,
                    'restore_lfs_shared_volume_required')
            temporary=path.with_name('.relink-'+path.name)
            try:
                os.link(shared,temporary)
                os.replace(temporary,path)
            finally:
                temporary.unlink(missing_ok=True)


def _write_restored_config(source, settings, postgres_dsn):
    """Retain policy, but bind only to the new isolated storage and loopback."""
    raw=tomllib.loads(source.read_text())
    server={'service_url':settings.service_url,'listen':'127.0.0.1','port':8042}
    for name in ('public_web_origin','temporary_ttl','transfer_ttl'):
        if name in raw.get('server',{}):
            server[name]=raw['server'][name]
    storage={'postgres_dsn':postgres_dsn,
             'content':str(settings.server.content_dir),
             'repositories':str(settings.server.repositories_dir),
             'blobs':str(settings.server.blob_dir),
             'staging':str(settings.server.staging_dir),
             'service_keys':str(settings.service_keys)}
    sections={'server':server,'storage':storage}
    for name in ('identity','money','limits','plugins','tools'):
        if name in raw:
            sections[name]=raw[name]
    lines=[]
    for section,values in sections.items():
        lines.append(f'[{section}]')
        for name,value in values.items():
            lines.append(f'{name} = {canonical(value).decode()}')
        lines.append('')
    recovery=raw.get('recovery',{})
    if recovery:
        lines.append('[recovery]')
        for name,value in recovery.items():
            if name!='custodians':
                lines.append(f'{name} = {canonical(value).decode()}')
        lines.append('')
        for custodian in recovery.get('custodians',()):
            lines.append('[[recovery.custodians]]')
            for name,value in custodian.items():
                lines.append(f'{name} = {canonical(value).decode()}')
            lines.append('')
    path=settings.config_dir/'msgd.toml'
    path.write_text('\n'.join(lines))
    os.chmod(path,0o600)


def _hash(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        while chunk:=source.read(65536):value.update(chunk)
    return value.hexdigest()


def _git_blob_digest(repository, oid):
    value=hashlib.sha256()
    size=0
    with subprocess.Popen(['git','--git-dir',str(repository),'cat-file','blob',oid],
                          stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as process:
        while chunk:=process.stdout.read(1024*1024):
            value.update(chunk);size+=len(chunk)
        status=process.wait()
    return status,size,value.hexdigest()


async def backup(app,destination):
    destination=Path(destination)
    require(not destination.exists() and not destination.is_symlink(),'backup_destination_exists')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='msg-backup-',dir=destination.parent) as tmp:
        directory=Path(tmp)
        async with app.metadata.transaction(write=True) as tx:
            # PostgreSQL's transaction-scoped deployment advisory lock excludes
            # other msgd writers while pg_dump uses its own MVCC connection.
            # Storage objects are published before their SQL pointers commit.
            await asyncio.to_thread(app.metadata.backup,directory/'metadata.dump')
            with psycopg.connect(app.settings.server.postgres_dsn) as connection:
                refs=_db_refs(connection)
            for source,name in ((app.settings.server.content_dir,'content'),
                                (app.settings.server.repositories_dir,'repositories'),
                                (app.settings.server.blob_dir,'blobs'),
                                (app.settings.server.staging_dir,'staging')):
                if source.exists():
                    _regular_tree(source)
                    await asyncio.to_thread(shutil.copytree,source,directory/name)
                else:
                    (directory/name).mkdir()
            _regular_tree(app.settings.service_keys)
            await asyncio.to_thread(shutil.copytree,app.settings.service_keys,directory/'service')
            require(all((directory/'service'/name).is_file() for name in _KEYS),
                    'service_keys_missing')
            require(app.settings.trust_file.is_file() and not app.settings.trust_file.is_symlink(),
                    'backup_trust_missing')
            shutil.copy2(app.settings.trust_file,directory/'root-public.json')
            # Keep policy/configuration for review. Restore rewrites storage paths,
            # disables Valkey and does not copy mail credentials.
            config_source=(app.settings.config_dir/'msgd.toml'
                           if (app.settings.config_dir/'msgd.toml').exists()
                           else app.settings.config_dir/'server.toml')
            require(config_source.is_file() and not config_source.is_symlink(),
                    'backup_configuration_missing')
            shutil.copy2(config_source,directory/'source-config.toml')
            _verify_storage(directory,refs)
        files={str(path.relative_to(directory)):_hash(path) for path in directory.rglob('*') if path.is_file()}
        manifest={'format':_FORMAT,'service_url':app.settings.service_url,'files':files,
                  'references':refs,'root_private_key_included':False,
                  'mail_credentials_included':False}
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


def restore(source,config_dir,data_dir,*,postgres_dsn='service=msgd'):
    """Restore only into new locations and an empty PostgreSQL database."""
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
            require(manifest.get('format')==_FORMAT and manifest.get('root_private_key_included') is False,
                    'invalid_backup')
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
        # Empty storage directories have no ZIP file entries.
        for name in ('content','repositories','blobs','staging'):
            (directory/name).mkdir(exist_ok=True)
        require((directory/'metadata.dump').is_file() and
                (directory/'root-public.json').is_file() and
                (directory/'source-config.toml').is_file() and
                all((directory/'service'/name).is_file() for name in _KEYS),
                'backup_required_file_missing')
        _verify_storage(directory,manifest['references'])
        fields=conninfo_to_dict(postgres_dsn)
        password=fields.pop('password',None)
        env=os.environ.copy()
        if password is not None:env['PGPASSWORD']=password
        safe_dsn=make_conninfo(**fields)
        with psycopg.connect(postgres_dsn) as connection:
            require(connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() LIMIT 1"
            ).fetchone() is None,'restore_database_not_empty')
        quarantine={'format':'msg-recovery-quarantine-v1','outbound_enabled':False,
                    'source_backup_sha256':_hash(Path(source)),
                    'revocation_replay':'required','authority':'health_only'}
        restore_dump(directory/'metadata.dump',safe_dsn=safe_dsn,env=env,
                     quarantine=quarantine)
        with psycopg.connect(postgres_dsn) as connection:
            require(_db_refs(connection)==manifest['references'],
                    'backup_database_reference_mismatch')
            row=connection.execute("SELECT value FROM settings WHERE key='runtime_config'").fetchone()
            runtime=loads(row[0]) if row is not None else {}
            require(type(runtime) is dict,'backup_runtime_config_invalid')
            runtime['accept_writes']=False
            runtime['cleanup_enabled']=False
            connection.execute('''INSERT INTO settings(key,value) VALUES('runtime_config',%s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value''',
                (canonical(runtime).decode(),))
        data_dir.mkdir(mode=0o700)
        settings=write_example(config_dir,data_dir,manifest['service_url'],postgres_dsn=postgres_dsn)
        # Install the fail-closed marker as soon as a runnable config exists,
        # including if a later filesystem verification fails.
        durable_write(config_dir/'recovery-drill.json',canonical({
            **quarantine,'format':'msg-recovery-drill-v1'}),mode=0o600)
        _write_restored_config(directory/'source-config.toml',settings,postgres_dsn)
        settings=load_settings(config_dir)
        for name,target in (('content',settings.server.content_dir),
                            ('repositories',settings.server.repositories_dir),
                            ('blobs',settings.server.blob_dir),
                            ('staging',settings.server.staging_dir)):
            if (directory/name).exists():
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.move(str(directory/name),target)
        settings.service_keys.parent.mkdir(parents=True,exist_ok=True)
        shutil.move(str(directory/'service'),settings.service_keys)
        os.chmod(settings.service_keys,0o700)
        settings.trust_file.parent.mkdir(parents=True,exist_ok=True)
        from msg.security.trust_files import write_trust
        write_trust(settings.config_dir,loads((directory/'root-public.json').read_bytes()),
                    writer=durable_write)
        _relink_lfs(data_dir,settings)
        _verify_storage(data_dir,manifest['references'],settings=settings)
    return {'status':'restored','root_admin_material':'restore_separate_encrypted_root_backup_locally',
            'mail':'disabled_until_configured','outbound':'disabled_recovery_drill',
            'revocation_replay':'required','promotion':'blocked'}
