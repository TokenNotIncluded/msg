"""Read-only deployment inspection and disposable, real-operation self-tests."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
import os
from pathlib import Path
import shutil
import psycopg
import stat
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import quote
from psycopg import sql

from msg.core.codec import canonical, decode, digest, loads, unb64, wire, b64
from msg.core.errors import Failure, require
from msg.core.models import Certificate, Resource, ResourceRef, Subject, AuditEvent
from msg.constants import ROOT_SUBJECT


@contextmanager
def temporary_postgres():
    """Run selftest against a new local cluster, never an installed database."""
    configured=os.environ.get('MSG_TEST_POSTGRES_URL_TEMPLATE')
    if configured:
        name='msg_selftest_'+uuid.uuid4().hex
        admin=configured.format(database='postgres')
        with psycopg.connect(admin,autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            yield configured.format(database=name)
        finally:
            with psycopg.connect(admin,autocommit=True) as connection:
                connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        return
    with tempfile.TemporaryDirectory(prefix='msg-selftest-pg-',dir='/tmp') as temporary:
        root=Path(temporary)
        data=root/'data'
        socket=root/'socket';socket.mkdir()
        subprocess.run(['initdb','-D',str(data),'-A','trust','--no-instructions'],
                       check=True,capture_output=True,text=True)
        try:
            subprocess.run(['pg_ctl','-D',str(data),'-l',str(root/'postgres.log'),
                            '-o',f"-k {socket} -h '' -p 5432",'-w','start'],
                           check=True,capture_output=True,text=True)
            yield f'postgresql://localhost:5432/postgres?host={quote(str(socket),safe="")}'
        finally:
            subprocess.run(['pg_ctl','-D',str(data),'-m','immediate','-w','stop'],
                           check=False,capture_output=True,text=True)


class ReadOnlyStore:
    """Inspect PostgreSQL without schema creation or repair."""
    def __init__(self,dsn):
        self.dsn=dsn

    async def inspect(self, callback):
        from msg.storage.postgres import PostgresMetadataStore
        store=PostgresMetadataStore(self.dsn,initialize=False)
        async with store.transaction(write=False) as session:
            return await callback(session)


def doctor(config_dir=Path('/etc/msgd'), *, clock=None):
    """This synchronous wrapper is also safe to call from an existing event loop."""
    # Running the read-only coroutine in a short-lived thread avoids nesting an
    # asyncio.run() inside adapters/tests with an already running event loop.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_doctor(config_dir,clock))).result()


async def _doctor(config_dir,clock):
    from msg.application import Application
    from msg.config import load_settings
    from msg.bootstrap import manifest
    from msg.security.certificates import CertificateValidator
    checks={}
    warnings=[]
    def success(name,**data):checks[name]={'ok':True,**data}
    def failed(name,code,**data):checks[name]={'ok':False,'code':code,**data}
    path=Path(config_dir)
    if not (path/'server.toml').is_file():
        return {'ok':False,'root_id':ROOT_SUBJECT,'checks':{'configuration':{'ok':False,'code':'configuration_missing'}}}
    try:
        settings=load_settings(path)
        app=Application(settings,clock=clock)
        success('configuration')
    except (Failure,ValueError,KeyError,OSError) as exc:
        failed('configuration',getattr(exc,'code','invalid_configuration'))
        return {'ok':False,'root_id':ROOT_SUBJECT,'checks':checks}
    if sys.version_info[:2]>=(3,15):success('python',version='.'.join(map(str,sys.version_info[:3])))
    else:failed('python','python_315_required',actual='.'.join(map(str,sys.version_info[:3])))
    missing=[name for name in ('cryptography','starlette','uvicorn','httpx','jsonschema','aiohttp','dns','graphql','psycopg','valkey') if find_spec(name) is None]
    if not missing:success('dependencies')
    else:failed('dependencies','dependency_unavailable',missing=missing)
    if shutil.which('git'):success('git')
    else:failed('git','git_missing')
    if not shutil.which('bwrap'):
        warnings.append({'code':'tool_isolation_unavailable','effect':'network_tool_jobs_fail_closed'})
    try:
        trust=loads(settings.trust_file.read_bytes())
        root=decode(Certificate,trust['certificate'])
        validator=CertificateValidator(app.registry,root,unb64(trust['public_key']),settings.service_url,
                                       clock or (lambda:datetime.now(UTC)))
        async def inspect(tx):
            require(tx.one('SELECT version FROM schema_version')[0]==1,'schema_version_unknown')
            success('storage')
            try:
                await validator.validate(root.resource_id,tx)
                require((await tx.subject(ROOT_SUBJECT)).local_only,'root_policy_corrupt')
                success('root_trust',fingerprint=digest(unb64(trust['public_key'])))
            except Failure as exc:failed('root_trust',exc.code)
            try:
                online=tx.setting('online_ca_certificate')
                require(online is not None,'issuer_not_ready')
                await validator.validate(online,tx)
                success('online_ca')
            except Failure as exc:failed('online_ca',exc.code)
            drift=[]
            for expected in manifest()['resources']:
                try:
                    resource=await tx.resource(expected['id'])
                    for field in ('type','parent','owner','group','mode','name'):
                        desired=int(expected[field],8) if field=='mode' else expected[field]
                        if getattr(resource,field)!=desired:drift.append({'id':resource.id,'field':field})
                except Failure:drift.append({'id':expected['id'],'field':'missing'})
            if drift:failed('bootstrap','bootstrap_drift',drift=drift)
            else:success('bootstrap')
            previous=None
            for raw, in tx.execute('SELECT body FROM audit ORDER BY seq'):
                audit=loads(raw)
                entry_digest=audit.pop('entry_digest')
                require(audit.get('previous_digest')==previous and digest(audit)==entry_digest,'audit_chain_mismatch')
                previous=entry_digest
            success('audit',head=previous)
        await ReadOnlyStore(settings.server.postgres_dsn).inspect(inspect)
    except (Failure,OSError,ValueError,KeyError,psycopg.Error) as exc:
        failed('inspection',getattr(exc,'code','inspection_failed'))
    if settings.server.valkey_url:
        try:
            from msg.storage.valkey_bus import ValkeyOutboxSignal
            await asyncio.to_thread(ValkeyOutboxSignal(settings.server.valkey_url).client.ping)
            success('valkey')
        except Exception:
            failed('valkey','valkey_unavailable')
    else:
        checks['valkey']={'ok':True,'status':'disabled'}
    content=settings.server.content_dir
    if (content/'private.git'/'HEAD').is_file():success('content_layout')
    else:failed('content_layout','content_missing')
    if settings.server.staging_dir.exists():
        disk=shutil.disk_usage(settings.server.staging_dir)
        success('capacity',free_bytes=disk.free)
    protected=path/'root'
    try:
        mode=stat.S_IMODE(protected.stat().st_mode)
        require(mode==0o700 and protected.stat().st_uid==0,'unsafe_root_permissions')
        secret=protected/'key.json'
        try:
            require(stat.S_IMODE(secret.stat().st_mode)==0o600 and secret.stat().st_uid==0,'unsafe_root_permissions')
        except PermissionError:
            # The network service being unable to inspect a root-owned 0700
            # directory is the intended deployment boundary.
            pass
        success('root_private_boundary')
    except (Failure,OSError) as exc:failed('root_private_boundary',getattr(exc,'code','root_material_missing'))
    return {'ok':all(c['ok'] for c in checks.values()),'root_id':ROOT_SUBJECT,'checks':checks,'warnings':warnings}


async def selftest():
    """No production database, listener, root PIN or live trust state is touched."""
    from dataclasses import replace
    from msg.application import Application
    from msg.config import write_example
    from msg.admin.root import _provision,_approve_csr
    from msg.security.crypto import Ed25519Signer,subject_id
    from msg.core.requests import request_for
    from msg.core.models import CertificateRequest,Signature,Scope
    from msg.security.certificates import csr_body
    from msg.security.capabilities import grant_for
    checks={}
    with tempfile.TemporaryDirectory(prefix='msg-selftest-') as temporary, temporary_postgres() as dsn:
        folder=Path(temporary)
        now=datetime.now(UTC)
        app=Application(write_example(folder/'etc',folder/'data','http://selftest.invalid',
                                      postgres_dsn=dsn),clock=lambda:now)
        try:
            csr,root=await _provision(app,'selftest-'+os.urandom(24).hex())
            await _approve_csr(app,csr,root,expected_digest=None,operator='isolated-selftest')
            async def call(name,args,key=None,subject=None,certs=(),expected=(),request_id=None):
                packet=request_for(name,args,app.settings.service_url,subject=subject,signer=key,
                    certificates=certs,expected=expected,expires_at=now+timedelta(seconds=120),request_id=request_id)
                return await app.executor.execute(packet)
            async def register(name):
                key=Ed25519Signer.generate();uid=subject_id(key.public_key)
                result=await call('identity.register',{'handle':name,'public_key':b64(key.public_key)},key,uid)
                require(result.status=='ok','selftest_registration_failed')
                return key,uid
            async def approve(key,uid,grants):
                csr=CertificateRequest(resource_id='pending',applicant=uid,subject_id=uid,requested_issuer=ROOT_SUBJECT,
                    public_key=key.public_key,kind='capability',grants=tuple(grants),issuance=None,
                    requested_ttl_seconds=600,target_service=app.settings.service_url,delegation_depth=0,authority_sources=(),
                    request_digest='',possession_proof=Signature(key_id=key.key_id,algorithm='ed25519',value=b''))
                proof=key.sign(canonical(csr_body(csr)),purpose='csr')
                result=await call('cert.request',{'requested_issuer':ROOT_SUBJECT,'public_key':b64(key.public_key),
                    'kind':'capability','grants':wire(grants),'requested_ttl_seconds':600,'possession_proof':wire(proof)},key,uid)
                require(result.status=='ok','selftest_csr_failed')
                return await _approve_csr(app,result.data['csr_id'],root,expected_digest=result.data['request_digest'],operator='isolated-selftest')
            alice,ua=await register('alice');bob,ub=await register('bob')
            post=await call('content.post_create',{'parent':'/tmp','body':'retained source bytes\r\n'},alice,ua,request_id='same-write')
            repeat=await call('content.post_create',{'parent':'/tmp','body':'retained source bytes\r\n'},alice,ua,request_id='same-write')
            checks['idempotency']=repeat.replayed and repeat.resources==post.resources
            rejected=await call('content.archive',{'id':post.resources[0].id},bob,ub,
                expected=((post.resources[0].id,post.data['generation']),))
            checks['sticky']=rejected.error is not None and rejected.error.code=='sticky_denied'
            denied=await call('discovery.get',{'id':'/private'},bob,ub)
            checks['deny']=denied.error is not None and denied.error.code=='permission_denied'
            denied=await call('content.post_create',{'parent':'/certified','body':'gate'},bob,ub)
            grant=grant_for(app.registry.capability('resource.certified_write'),scope=Scope(resource_id='t_certified',descendants=True),
                operations=('content.post_create@1',))
            cert=await approve(bob,ub,(grant,))
            accepted=await call('content.post_create',{'parent':'/certified','body':'gate'},bob,ub,(cert.resource_id,))
            checks['certgate']=denied.error is not None and denied.error.code=='certificate_gate' and accepted.status=='ok'
            wrong=grant_for(app.registry.capability('resource.certified_write'),scope=Scope(resource_id='t_main',descendants=True),
                operations=('content.post_create@1',))
            wrong_cert=await approve(alice,ua,(wrong,))
            rejected=await call('content.post_create',{'parent':'/certified','body':'wrong scope'},alice,ua,(wrong_cert.resource_id,))
            checks['certificate_scope']=rejected.error is not None and rejected.error.code=='certificate_gate'
            manage=grant_for(app.registry.capability('group.manage_override'),scope=Scope(resource_id='g_admins'),
                operations=('group.member.add@1','group.member.remove@1'))
            manager=await approve(alice,ua,(manage,))
            await call('group.member.add',{'group':'g_admins','subject':ub},alice,ua,(manager.resource_id,))
            child=await call('content.topic_create',{'parent':'/admins','name':'selftest'},bob,ub)
            require(child.status=='ok','selftest_group_failed')
            async with app.metadata.transaction(write=False) as tx:
                resource=await tx.resource(child.resources[0].id)
                checks['setgid']=resource.group=='g_admins' and bool(resource.mode&0o2000)
            await call('group.member.remove',{'group':'g_admins','subject':ub},alice,ua,(manager.resource_id,))
            rejected=await call('discovery.get',{'id':child.resources[0].id},bob,ub)
            checks['membership_revocation']=rejected.error is not None
            async with app.metadata.transaction(write=True) as tx:
                tx.execute('UPDATE certificates SET revoked=1 WHERE id=?',(cert.resource_id,),write=True)
            rejected=await call('content.post_create',{'parent':'/certified','body':'revoked'},bob,ub,(cert.resource_id,))
            checks['certificate_revocation']=rejected.error is not None and rejected.error.code=='certificate_revoked'
            rejected=await call('discovery.get',{'id':'/private'},root,ROOT_SUBJECT)
            checks['root_network_rejected']=rejected.error is not None and rejected.error.code=='local_only'
            all_versions=await call('discovery.get',{'id':post.resources[0].id,'view':'meta'})
            checks['stable_id_read']=all_versions.status=='ok'
        except Failure as exc:
            checks['failure']={'code':exc.code}
        finally:
            await app.close()
    return {'ok':bool(checks) and all(value is True for value in checks.values()),'checks':checks,'cleaned_up':not folder.exists()}
