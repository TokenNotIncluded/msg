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


def feature_results(features, observations, field):
    """Link manifest entries to checks actually run; never synthesize a pass."""
    result={}
    for feature in features:
        feature_id=feature['feature_id']
        name=feature[field]
        if not feature['enabled_by_default']:
            result[feature_id]={'status':'disabled','check':name}
        elif name is None:
            result[feature_id]={'status':'skip','check':None}
        elif name not in observations:
            result[feature_id]={'status':'fail','check':name,'reason':'check_missing'}
        else:
            value=observations[name]
            if isinstance(value,dict):
                status=('skip' if value.get('status')=='disabled' else
                        'pass' if value.get('ok') is True else 'fail')
            else:
                status='pass' if value is True else 'fail'
            result[feature_id]={'status':status,'check':name}
    return result


async def authority_snapshot_drift(app, root, online, tx):
    """Show signed authority missing from this installation's current vocabulary.

    Registry changes are never silently copied into a signed trust anchor or CA.
    Compare individual operations, because a capability can gain an operation
    without changing its name or version. Scope and constraints are checked as
    well; a narrower old grant must not be described as covering a broad one.
    """
    from msg.security.policy import constraints_subset, scope_subset
    require(root.issuance is not None and online.issuance is not None,'issuer_not_ca')

    async def missing(expected, actual):
        gaps=[]
        for desired in expected:
            covered=set()
            for grant in actual:
                if (grant.capability==desired.capability and grant.version==desired.version and
                    await scope_subset(desired.scope,grant.scope,tx) and
                    constraints_subset(desired.constraints,grant.constraints)):
                    covered.update(grant.operations)
            operations=sorted(desired.operations-covered)
            if operations:
                gaps.append({'capability':desired.capability,'version':desired.version,
                             'scope':wire(desired.scope),'operations':operations})
        return gaps

    primary=app.primary_ceiling()
    base=app.base_grants()
    return {'root_use':await missing(primary,root.grants),
            'root_issue':await missing(primary,root.issuance.issue_grants),
            'online_issue':await missing(base,online.issuance.issue_grants)}


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
    from msg.bootstrap import manifest,feature_manifest
    from msg.security.certificates import CertificateValidator
    checks={}
    warnings=[]
    def success(name,**data):checks[name]={'ok':True,**data}
    def failed(name,code,**data):checks[name]={'ok':False,'code':code,**data}
    path=Path(config_dir)
    try:
        settings=load_settings(path)
        app=Application(settings,clock=clock)
        success('configuration')
        success('money_config',enabled=settings.money.enabled,
                currency_id=settings.money.currency_id,
                display_name=settings.money.display_name,code=settings.money.code,
                scale=settings.money.scale,transfer_fee=settings.money.transfer_fee,
                allow_overdraft=settings.money.allow_overdraft)
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
                from msg.admin.market_check import inspect_clearing
                success('market_clearing',**inspect_clearing(app,tx))
            except (Failure,OSError,ValueError,KeyError) as exc:
                failed('market_clearing',getattr(exc,'code','market_inspection_failed'))
            try:
                from msg.core.requests import SECRET_DELIVERY_MIN_VERSION
                columns={row[0] for row in tx.rows("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='token_deliveries'")}
                require({'credential_id','subject','request_id','request_digest','recovery_verifier',
                         'recovery_expires_at','claimed_at','consumed_at'} <= columns,
                        'credential_delivery_schema_missing')
                for operation,version in SECRET_DELIVERY_MIN_VERSION.items():
                    require(app.registry.operation(operation,version).effect=='transaction',
                            'credential_delivery_contract_invalid')
                success('credential_delivery',recovery_window_seconds=settings.credential_delivery_recovery_window,
                        release='once',secret_url=False,versions=SECRET_DELIVERY_MIN_VERSION)
            except Failure as exc:failed('credential_delivery',exc.code)
            try:
                await validator.validate(root.resource_id,tx)
                require((await tx.subject(ROOT_SUBJECT)).local_only,'root_policy_corrupt')
                success('root_trust',fingerprint=digest(unb64(trust['public_key'])))
            except Failure as exc:failed('root_trust',exc.code)
            try:
                online=tx.setting('online_ca_certificate')
                require(online is not None,'issuer_not_ready')
                online_certificate=await validator.validate(online,tx)
                from msg.security.crypto import Ed25519Signer
                online_key=Ed25519Signer.from_bytes((settings.service_keys/'online.key').read_bytes())
                require(online_certificate.key_id==online_key.key_id,'issuer_key_mismatch')
                success('online_ca')
            except (Failure,OSError,ValueError) as exc:failed('online_ca',getattr(exc,'code','issuer_key_missing'))
            try:
                online_id=tx.setting('online_ca_certificate')
                require(online_id is not None,'issuer_not_ready')
                online_certificate=await tx.certificate(online_id)
                gaps=await authority_snapshot_drift(app,root,online_certificate,tx)
                if any(gaps.values()):
                    failed('authority_snapshot','signed_authority_outdated',missing=gaps,
                           action='No automatic authority expansion. Preserve this chain and keep the new '
                                  'operation unavailable, or explicitly rotate the root and reissue '
                                  'certificates; rotation invalidates old chains.')
                else:
                    success('authority_snapshot')
            except Failure as exc:
                failed('authority_snapshot',exc.code)
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
    from msg.admin.root import root_envelope
    protected=root_envelope(path).parent
    if protected==path/'root':
        warnings.append({'code':'legacy_root_storage','effect':'migrate_to_var_lib_msgd_root'})
    try:
        require(not ((path/'root'/'key.json').exists() and
                     (settings.root_private_dir/'key.json').exists()),
                'duplicate_root_material')
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
    features=feature_results(feature_manifest(),checks,'doctor_check')
    return {'ok':all(c['ok'] for c in checks.values()) and
            all(row['status']!='fail' for row in features.values()),
            'root_id':ROOT_SUBJECT,'checks':checks,'features':features,'warnings':warnings}


async def _selftest_ca_chain(app,root,call,register,now):
    """Publish a real, narrowly scoped CA chain in the disposable test instance."""
    from dataclasses import replace
    from msg.core.models import CertificateRequest, IssuancePolicy, Scope, Signature
    from msg.plugins.common import new_id
    from msg.security.capabilities import grant_for
    from msg.security.certificates import csr_body, sign_certificate
    from msg.admin.root import _approve_csr

    scope=Scope(resource_id=app.namespace_root,descendants=True)
    issue=grant_for(app.registry.capability('cert.issue'),scope=scope,
                    operations=('cert.publish@1',))
    ca_issue=grant_for(app.registry.capability('cert.ca.issue'),scope=scope,
                       operations=('cert.publish@1',))
    read=grant_for(app.registry.capability('discovery.basic'),scope=scope,
                   operations=('discovery.get@1',))
    bounded_tool=replace(grant_for(app.registry.capability('tool.use'),
        scope=Scope(resource_id='t_tools'),operations=('tool.run@1',)),
        constraints={'hosts':['example.org']})
    ca_grants=(issue,ca_issue)
    issuable=(issue,ca_issue,read,bounded_tool)

    async def request_certificate(key,uid,issuer,kind,grants,policy=None,ttl=600):
        csr=CertificateRequest(resource_id='pending',applicant=uid,subject_id=uid,
            requested_issuer=issuer,public_key=key.public_key,kind=kind,grants=tuple(grants),
            issuance=policy,requested_ttl_seconds=ttl,target_service=app.settings.service_url,
            delegation_depth=0,authority_sources=(),request_digest='',
            possession_proof=Signature(key_id=key.key_id,algorithm='ed25519',value=b''))
        proof=key.sign(canonical(csr_body(csr)),purpose='csr')
        result=await call('cert.request',{'requested_issuer':issuer,'public_key':b64(key.public_key),
            'kind':kind,'grants':wire(tuple(grants)),'issuance':wire(policy),
            'requested_ttl_seconds':ttl,'possession_proof':wire(proof)},key,uid)
        require(result.status=='ok',result.error.code if result.error else 'selftest_csr_failed')
        return result

    async def publish(parent,parent_key,parent_uid,key,uid,kind,grants,policy=None,
                      *,ttl=600,target_service=None):
        requested=await request_certificate(key,uid,parent_uid,kind,grants,policy,ttl)
        cert=Certificate(resource_id=new_id('cert'),serial=new_id('serial'),subject_id=uid,
            key_id=key.key_id,issuer_id=parent_uid,parent_certificate_id=parent.resource_id,
            authority_sources=(),kind=kind,grants=tuple(grants),not_before=now,
            expires_at=now+timedelta(seconds=ttl),
            target_service=target_service or app.settings.service_url,
            delegation_depth=0,issuance=policy,
            signature=Signature(key_id=parent_key.key_id,algorithm='ed25519',value=b''))
        cert=sign_certificate(cert,parent_key)
        result=await call('cert.publish',{'csr_id':requested.data['csr_id'],
            'certificate':wire(cert)},parent_key,parent_uid,(parent.resource_id,))
        return result,cert,requested

    l1_key,l1_uid=await register('test-l1')
    l1_policy=IssuancePolicy(issue_grants=issuable,max_cert_ttl_seconds=600,
                             max_child_ca_depth=2,max_delegation_depth=0)
    l1_request=await request_certificate(l1_key,l1_uid,ROOT_SUBJECT,'ca',ca_grants,l1_policy)
    l1=await _approve_csr(app,l1_request.data['csr_id'],root,
                          expected_digest=l1_request.data['request_digest'],operator='isolated-selftest')
    l2_key,l2_uid=await register('test-l2')
    l2_policy=IssuancePolicy(issue_grants=issuable,max_cert_ttl_seconds=600,
                             max_child_ca_depth=1,max_delegation_depth=0)
    l2_result,l2,_=await publish(l1,l1_key,l1_uid,l2_key,l2_uid,'ca',ca_grants,l2_policy)
    require(l2_result.status=='ok',l2_result.error.code if l2_result.error else 'selftest_l2_failed')
    l3_key,l3_uid=await register('test-l3')
    l3_policy=IssuancePolicy(issue_grants=issuable,max_cert_ttl_seconds=600,
                             max_child_ca_depth=0,max_delegation_depth=0)
    l3_result,l3,_=await publish(l2,l2_key,l2_uid,l3_key,l3_uid,'ca',ca_grants,l3_policy)
    require(l3_result.status=='ok',l3_result.error.code if l3_result.error else 'selftest_l3_failed')
    leaf_key,leaf_uid=await register('test-leaf')
    leaf_result,leaf,_=await publish(l3,l3_key,l3_uid,leaf_key,leaf_uid,'capability',(read,))
    require(leaf_result.status=='ok',leaf_result.error.code if leaf_result.error else 'selftest_leaf_failed')
    async with app.metadata.transaction(write=False) as tx:
        for cert in (l1,l2,l3,leaf):
            await app.certificates.validate(cert.resource_id,tx)
        paths=[await tx.path(cert.resource_id) for cert in (l1,l2,l3,leaf)]
        scope_ok=True
        for cert in (l1,l2,l3,leaf):
            for grant in (*cert.grants,*(cert.issuance.issue_grants if cert.issuance else ())):
                if grant.scope.resource_id==app.namespace_root:
                    continue
                if app.namespace_root not in {item.id for item in await tx.ancestors(grant.scope.resource_id)}:
                    scope_ok=False
    test_path='/_test/'+app.selftest_run_id
    valid=all(path.startswith(test_path+'/') for path in paths) and scope_ok

    l4_key,l4_uid=await register('test-l4')
    l4_result,l4,l4_request=await publish(l3,l3_key,l3_uid,l4_key,l4_uid,'ca',ca_grants,l3_policy)
    async with app.metadata.transaction(write=False) as tx:
        pending=(await tx.csr_state(l4_request.data['csr_id'])).status=='pending'
        absent=tx.one('SELECT 1 FROM certificates WHERE id=?',(l4.resource_id,)) is None
    denied=(l4_result.error is not None and l4_result.error.code=='ca_depth_exceeded'
            and pending and absent)

    out_of_scope=grant_for(app.registry.capability('discovery.basic'),
        scope=Scope(resource_id='r_root',descendants=True),operations=('discovery.get@1',))
    bad_scope_key,bad_scope_uid=await register('test-bad-scope')
    bad_scope,bad_scope_cert,bad_scope_request=await publish(
        l1,l1_key,l1_uid,bad_scope_key,bad_scope_uid,'capability',(out_of_scope,))
    async with app.metadata.transaction(write=False) as tx:
        scope_pending=(await tx.csr_state(bad_scope_request.data['csr_id'])).status=='pending'
        scope_absent=tx.one('SELECT 1 FROM certificates WHERE id=?',(bad_scope_cert.resource_id,)) is None
    denied_scope=(bad_scope.error is not None and bad_scope.error.code=='issuance_scope_exceeded'
                  and scope_pending and scope_absent)

    broad_read=grant_for(app.registry.capability('discovery.basic'),scope=scope,
        operations=('discovery.get@1','discovery.list@1'))
    bad_ops_key,bad_ops_uid=await register('test-bad-ops')
    bad_ops,bad_ops_cert,bad_ops_request=await publish(
        l1,l1_key,l1_uid,bad_ops_key,bad_ops_uid,'capability',(broad_read,))
    async with app.metadata.transaction(write=False) as tx:
        ops_pending=(await tx.csr_state(bad_ops_request.data['csr_id'])).status=='pending'
        ops_absent=tx.one('SELECT 1 FROM certificates WHERE id=?',(bad_ops_cert.resource_id,)) is None
    denied_ops=(bad_ops.error is not None and bad_ops.error.code=='issuance_scope_exceeded'
                and ops_pending and ops_absent)

    broad_tool=replace(bounded_tool,constraints={'hosts':['example.org','other.example']})
    bad_constraints_key,bad_constraints_uid=await register('test-bad-constraints')
    bad_constraints,bad_constraints_cert,bad_constraints_request=await publish(
        l1,l1_key,l1_uid,bad_constraints_key,bad_constraints_uid,'capability',(broad_tool,))
    async with app.metadata.transaction(write=False) as tx:
        constraints_pending=(await tx.csr_state(bad_constraints_request.data['csr_id'])).status=='pending'
        constraints_absent=tx.one('SELECT 1 FROM certificates WHERE id=?',
                                  (bad_constraints_cert.resource_id,)) is None
    denied_constraints=(bad_constraints.error is not None and
                        bad_constraints.error.code=='issuance_scope_exceeded' and
                        constraints_pending and constraints_absent)

    bad_ttl_key,bad_ttl_uid=await register('test-bad-ttl')
    bad_ttl,bad_ttl_cert,bad_ttl_request=await publish(
        l1,l1_key,l1_uid,bad_ttl_key,bad_ttl_uid,'capability',(read,),ttl=601)
    async with app.metadata.transaction(write=False) as tx:
        ttl_pending=(await tx.csr_state(bad_ttl_request.data['csr_id'])).status=='pending'
        ttl_absent=tx.one('SELECT 1 FROM certificates WHERE id=?',(bad_ttl_cert.resource_id,)) is None
    denied_ttl=(bad_ttl.error is not None and bad_ttl.error.code in
                {'certificate_validity_escalation','certificate_ttl_escalation'} and
                ttl_pending and ttl_absent)

    bad_service_key,bad_service_uid=await register('test-bad-service')
    bad_service,bad_service_cert,bad_service_request=await publish(
        l1,l1_key,l1_uid,bad_service_key,bad_service_uid,'capability',(read,),
        target_service='https://wrong-service.invalid')
    async with app.metadata.transaction(write=False) as tx:
        service_pending=(await tx.csr_state(bad_service_request.data['csr_id'])).status=='pending'
        service_absent=tx.one('SELECT 1 FROM certificates WHERE id=?',(bad_service_cert.resource_id,)) is None
    denied_service=(bad_service.error is not None and bad_service.error.code=='csr_certificate_mismatch'
                    and service_pending and service_absent)

    bad_policy=IssuancePolicy(issue_grants=(*issuable,broad_read),max_cert_ttl_seconds=600,
                              max_child_ca_depth=1,max_delegation_depth=0)
    bad_policy_key,bad_policy_uid=await register('test-bad-issue-grants')
    bad_issue_grants,bad_policy_cert,bad_policy_request=await publish(
        l1,l1_key,l1_uid,bad_policy_key,bad_policy_uid,'ca',ca_grants,bad_policy)
    async with app.metadata.transaction(write=False) as tx:
        policy_pending=(await tx.csr_state(bad_policy_request.data['csr_id'])).status=='pending'
        policy_absent=tx.one('SELECT 1 FROM certificates WHERE id=?',(bad_policy_cert.resource_id,)) is None
    denied_policy=(bad_issue_grants.error is not None and
                   bad_issue_grants.error.code=='issuance_scope_exceeded' and
                   policy_pending and policy_absent)

    child_key,child_uid=await register('test-leaf-child')
    leaf_issue,leaf_child,leaf_request=await publish(
        leaf,leaf_key,leaf_uid,child_key,child_uid,'capability',(read,))
    async with app.metadata.transaction(write=False) as tx:
        leaf_pending=(await tx.csr_state(leaf_request.data['csr_id'])).status=='pending'
        leaf_absent=tx.one('SELECT 1 FROM certificates WHERE id=?',(leaf_child.resource_id,)) is None
    leaf_denied=(leaf_issue.error is not None and leaf_pending and leaf_absent)

    revoked=True
    for ancestor in (l1,l2,l3):
        async with app.metadata.transaction(write=True) as tx:
            tx.execute('UPDATE certificates SET revoked=1 WHERE id=?',(ancestor.resource_id,),write=True)
        async with app.metadata.transaction(write=False) as tx:
            try:
                await app.certificates.validate(leaf.resource_id,tx)
            except Failure as exc:
                revoked=revoked and exc.code=='certificate_revoked'
            else:
                revoked=False
        async with app.metadata.transaction(write=True) as tx:
            tx.execute('UPDATE certificates SET revoked=0 WHERE id=?',(ancestor.resource_id,),write=True)
    return {'ca_l1_l2_l3_leaf':valid,'ca_l3_cannot_issue_ca':denied,
            'ca_ancestor_revocation':revoked,'ca_scope_expansion_denied':denied_scope,
            'ca_operations_expansion_denied':denied_ops,
            'ca_constraints_expansion_denied':denied_constraints,
            'ca_ttl_expansion_denied':denied_ttl,
            'ca_target_service_change_denied':denied_service,
            'ca_issue_grants_expansion_denied':denied_policy,
            'ca_leaf_cannot_issue':leaf_denied}


async def selftest():
    """No production database, listener, root PIN or live trust state is touched."""
    from dataclasses import replace
    from msg.application import Application
    from msg.config import write_example
    from msg.admin.root import _provision,_approve_csr
    from msg.security.crypto import Ed25519Signer,subject_id
    from msg.security.age_keys import generate_age_key
    from msg.core.requests import request_for
    from msg.core.models import CertificateRequest,Signature,Scope
    from msg.security.certificates import csr_body
    from msg.security.capabilities import grant_for
    from msg.bootstrap import feature_manifest
    checks={}
    with tempfile.TemporaryDirectory(prefix='msg-selftest-') as temporary, temporary_postgres() as dsn:
        folder=Path(temporary)
        now=datetime.now(UTC)
        run_id=uuid.uuid4().hex
        test_path='/_test/'+run_id
        app=Application(write_example(folder/'etc',folder/'data','https://selftest.invalid',
                                      postgres_dsn=dsn),clock=lambda:now,selftest_run_id=run_id)
        try:
            csr,root=await _provision(app,'selftest-'+os.urandom(24).hex())
            await _approve_csr(app,csr,root,expected_digest=None,operator='isolated-selftest')
            async with app.metadata.transaction(write=False) as tx:
                namespace=app.namespace_root
                require(await tx.path(namespace)==test_path,'selftest_namespace_missing')
                root_scoped=True
                for (resource_id,) in tx.execute('SELECT id FROM resources'):
                    if resource_id in {'r_root','t_selftest'}:
                        continue
                    ancestors=await tx.ancestors(resource_id)
                    if resource_id!=namespace and namespace not in {item.id for item in ancestors}:
                        root_scoped=False
                        break
                checks['isolated_namespace']=root_scoped
            async def call(name,args,key=None,subject=None,certs=(),expected=(),request_id=None,
                           contract_version=1):
                packet=request_for(name,args,app.settings.service_url,subject=subject,signer=key,
                    certificates=certs,expected=expected,expires_at=now+timedelta(seconds=120),
                    request_id=request_id,contract_version=contract_version)
                return await app.executor.execute(packet)
            async def register(name):
                key=Ed25519Signer.generate();uid=subject_id(key.public_key)
                _,recipient=generate_age_key()
                result=await call('identity.register',{'handle':name,'public_key':b64(key.public_key),
                    'encryption_recipient':recipient},key,uid,contract_version=2)
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
            post=await call('content.post_create',{'parent':test_path+'/tmp','body':'retained source bytes\r\n'},alice,ua,request_id='same-write')
            require(post.status=='ok',post.error.code if post.error else 'selftest_post_failed')
            # OnlineIssuer exercises only the independent temporary Test Root
            # created above. No production trust material or signer is opened.
            async with app.metadata.transaction(write=False) as tx:
                identity_cert=next(decode(Certificate,loads(raw)) for (raw,) in
                    tx.execute('SELECT body FROM certificates WHERE subject=?',(ua,))
                    if decode(Certificate,loads(raw)).kind=='identity')
                issued_audits=[loads(raw) for (raw,) in tx.execute('SELECT body FROM audit ORDER BY seq')
                               if loads(raw)['event']['type']=='cert.auto.issue']
            registration_audit=next((entry for entry in issued_audits if
                entry['event']['data']['certificate_id']==identity_cert.resource_id),None)
            checks['online_registration']=bool(registration_audit and
                registration_audit['event']['data']['automatic'] is True and
                registration_audit['event']['data']['authority_source']['key_id']==alice.key_id)
            ordinary=grant_for(app.registry.capability('discovery.basic'),
                scope=Scope(resource_id=post.resources[0].id),operations=('discovery.get@1',))
            delegated=await call('identity.delegate',{'grantee':ub,'key_id':bob.key_id,
                'grants':wire((ordinary,)),'ttl':600},alice,ua,request_id='online-selftest-delegation')
            require(delegated.status=='ok','selftest_online_delegation_failed')
            async with app.metadata.transaction(write=False) as tx:
                audit_rows=[loads(raw) for (raw,) in tx.execute('SELECT body FROM audit ORDER BY seq')]
            delegation_audit=next((entry for entry in audit_rows if
                entry['event']['type']=='cert.auto.issue' and delegated.status=='ok' and
                entry['event']['data']['certificate_id']==delegated.data['certificate_id']),None)
            checks['online_delegation']=bool(delegation_audit and
                delegation_audit['event']['data']['authority_source']['id']==delegated.resources[0].id and
                delegation_audit['event']['data']['grant_digest']==digest((ordinary,)))
            renewed=await call('identity.certificate_renew',{},alice,ua)
            overlong=await call('identity.certificate_renew',{'ttl':31536000},alice,ua)
            checks['online_renewal']=(renewed.status=='ok' and overlong.error is not None and
                overlong.error.code=='renewal_ttl_exceeded')
            revoked_source=await call('identity.delegation_revoke',{'id':delegated.resources[0].id},alice,ua)
            async with app.metadata.transaction(write=False) as tx:
                derived=await app.certificates.validate(identity_cert.resource_id,tx)
                delegation_invalid=False
                try:await app.certificates.validate(delegated.data['certificate_id'],tx)
                except Failure as exc:delegation_invalid=exc.code=='authority_source_inactive'
            checks['online_source_revocation']=revoked_source.status=='ok' and derived.kind=='identity' and delegation_invalid
            repeat=await call('content.post_create',{'parent':test_path+'/tmp','body':'retained source bytes\r\n'},alice,ua,request_id='same-write')
            checks['idempotency']=repeat.replayed and repeat.resources==post.resources
            rejected=await call('content.archive',{'id':post.resources[0].id},bob,ub,
                expected=((post.resources[0].id,post.data['generation']),))
            checks['sticky']=rejected.error is not None and rejected.error.code=='sticky_denied'
            denied=await call('discovery.get',{'id':test_path+'/private'},bob,ub)
            checks['deny']=denied.error is not None and denied.error.code=='permission_denied'
            denied=await call('content.post_create',{'parent':test_path+'/certified','body':'gate'},bob,ub)
            grant=grant_for(app.registry.capability('resource.certified_write'),scope=Scope(resource_id='t_certified',descendants=True),
                operations=('content.post_create@1',))
            cert=await approve(bob,ub,(grant,))
            accepted=await call('content.post_create',{'parent':test_path+'/certified','body':'gate'},bob,ub,(cert.resource_id,))
            checks['certgate']=denied.error is not None and denied.error.code=='certificate_gate' and accepted.status=='ok'
            wrong=grant_for(app.registry.capability('resource.certified_write'),scope=Scope(resource_id='t_main',descendants=True),
                operations=('content.post_create@1',))
            wrong_cert=await approve(alice,ua,(wrong,))
            rejected=await call('content.post_create',{'parent':test_path+'/certified','body':'wrong scope'},alice,ua,(wrong_cert.resource_id,))
            checks['certificate_scope']=rejected.error is not None and rejected.error.code=='certificate_gate'
            manage=grant_for(app.registry.capability('group.manage_override'),scope=Scope(resource_id='g_admins'),
                operations=('group.member.add@1','group.member.remove@1'))
            manager=await approve(alice,ua,(manage,))
            await call('group.member.add',{'group':'g_admins','subject':ub},alice,ua,(manager.resource_id,))
            child=await call('content.topic_create',{'parent':test_path+'/admins','name':'selftest'},bob,ub)
            require(child.status=='ok','selftest_group_failed')
            async with app.metadata.transaction(write=False) as tx:
                resource=await tx.resource(child.resources[0].id)
                checks['setgid']=resource.group=='g_admins' and bool(resource.mode&0o2000)
            await call('group.member.remove',{'group':'g_admins','subject':ub},alice,ua,(manager.resource_id,))
            rejected=await call('discovery.get',{'id':child.resources[0].id},bob,ub)
            checks['membership_revocation']=rejected.error is not None
            async with app.metadata.transaction(write=True) as tx:
                tx.execute('UPDATE certificates SET revoked=1 WHERE id=?',(cert.resource_id,),write=True)
            rejected=await call('content.post_create',{'parent':test_path+'/certified','body':'revoked'},bob,ub,(cert.resource_id,))
            checks['certificate_revocation']=rejected.error is not None and rejected.error.code=='certificate_revoked'
            rejected=await call('discovery.get',{'id':test_path+'/private'},root,ROOT_SUBJECT)
            checks['root_network_rejected']=rejected.error is not None and rejected.error.code=='local_only'
            all_versions=await call('discovery.get',{'id':post.resources[0].id,'view':'meta'})
            checks['stable_id_read']=all_versions.status=='ok'
            checks.update(await _selftest_ca_chain(app,root,call,register,now))
            from msg.admin.upgrade_check import check_upgrade_recovery
            checks['identity_upgrade_recovery']=await check_upgrade_recovery(app,now)
            from msg.admin.token_delivery_check import check_token_delivery
            checks['credential_delivery_recovery']=await check_token_delivery(app,now)
            from msg.admin.market_check import check_market_e2e
            checks['market_e2e']=await check_market_e2e(app,root,call,register)
        except Failure as exc:
            checks['failure']={'code':exc.code}
        finally:
            await app.close()
    features=feature_results(feature_manifest(),checks,'selftest_case')
    return {'ok':bool(checks) and all(value is True for value in checks.values()) and
            all(row['status']!='fail' for row in features.values()),
            'checks':checks,'features':features,'cleaned_up':not folder.exists()}
