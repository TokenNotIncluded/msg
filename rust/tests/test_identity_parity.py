"""Check native authority reads against real Python services and Python-created SQLite.

Only deterministic, public test keys and disposable databases are used. This is
not a production probe. The native JSONL helper has no network listener.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from msg.core.codec import b64, canonical, digest, wire
from msg.core.errors import Failure
from msg.core.models import (
    CapabilityGrant,
    CapabilitySpec,
    Certificate,
    CertificateRequest,
    Credential,
    IssuancePolicy,
    OperationResult,
    OperationSpec,
    Resource,
    ResourceRef,
    Scope,
    SignatureProof,
    Subject,
    TokenProof,
)
from msg.core.packet import decode_packet
from msg.core.registry import Registry
from msg.core.requests import payload_fields, signing_bytes
from msg.core.schemas import NETWORK_CONSTRAINTS
from msg.oauth_config import SCOPES, OAuthClient, OAuthConfig
from msg.security.authentication import AuthenticationService
from msg.security.certificates import (
    CertificateValidator,
    certificate_body,
    csr_body,
    sign_certificate,
)
from msg.security.crypto import Ed25519Signer, key_id, subject_id
from msg.security.oauth import resource_execution
from msg.security.policy import allows, constraints_subset, scope_contains, scope_subset
from msg.security.token_delivery import recovery_verifier
from msg.storage.sqlite import SqliteMetadataStore

NOW = datetime(2026, 10, 5, tzinfo=UTC)
SERVICE = 'https://msg.test.invalid'
READ = 'discovery.get'
WRITE = 'content.post_edit'
OPS = frozenset({
    READ + '@1',
    WRITE + '@1',
    'identity.token_rotate@2',
    'identity.register@1',
    'identity.temporary@3',
    'identity.custodial_create@2',
    'identity.token_recover@1',
    'achievement.finish@1',
    'transfer.open@1',
    'tool.run@1',
})


async def unused(*_args):
    raise AssertionError('Conformance must never execute a business handler')


class Fixture:
    def __init__(self, path: Path):
        self.path = path
        self.metadata = SqliteMetadataStore(path)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.now = NOW
        self.signers = {
            name: Ed25519Signer.from_bytes(bytes([n]) * 32)
            for n, name in enumerate(('root', 'alice', 'bob', 'ca', 'online'), 1)
        }
        self.users = {name: subject_id(s.public_key) for name, s in self.signers.items()}
        self.users.update(root='u_root', online='u_online_ca')
        self.keys = {name: key_id(s.public_key) for name, s in self.signers.items()}
        self.registry = Registry()
        self.grants = {
            'resource.basic': self.grant('resource.basic', OPS),
            'identity.basic': self.grant('identity.basic', OPS),
            'cert.issue': self.grant('cert.issue', {'cert.publish@1'}),
            'cert.ca.issue': self.grant('cert.ca.issue', {'cert.publish@1'}),
            'tool.use': self.grant(
                'tool.use',
                {'tool.run@1'},
                constraints={
                    'hosts': ['example.test'],
                    'schemes': ['https'],
                    'ports': [443],
                    'methods': ['GET', 'HEAD'],
                    'max_response_bytes': 4096,
                    'timeout_ms': 5000,
                    'max_redirects': 2,
                },
            ),
        }
        self.primary = tuple(self.grants.values())
        for name, grant in self.grants.items():
            self.registry.add_capability(
                CapabilitySpec(
                    name=name,
                    version=1,
                    scope_types=frozenset({'space', 'topic', 'file', 'certificate', 'delegation'}),
                    operations=grant.operations,
                    replaces_checks=frozenset(),
                    delegatable=True,
                    ca_only=name.startswith('cert.'),
                    constraints_schema=ResourceRef(id='schema:network-constraints')
                    if name == 'tool.use'
                    else None,
                )
            )
        self.registry.add_schema(ResourceRef(id='schema:network-constraints'), NETWORK_CONSTRAINTS)
        self.resources = {}
        for rid, parent, kind in [
            ('r_root', None, 'space'),
            ('r_area', 'r_root', 'topic'),
            ('r_file', 'r_area', 'file'),
            ('r_other', 'r_root', 'topic'),
            ('r_delegation', 'r_root', 'delegation'),
            ('c_root', 'r_root', 'certificate'),
            ('c_ca', 'r_root', 'certificate'),
            ('c_leaf', 'r_root', 'certificate'),
            ('c_delegation', 'r_root', 'certificate'),
            ('c_online', 'r_root', 'certificate'),
            ('c_online_leaf', 'r_root', 'certificate'),
            ('c_network', 'r_root', 'certificate'),
            ('csr_new', 'r_root', 'certificate'),
            ('c_new', 'r_root', 'certificate'),
        ]:
            resource = Resource(
                id=rid,
                type=kind,
                type_version=1,
                name=rid,
                parent=parent,
                owner=self.users['bob'],
                group='g_test',
                mode=0o755,
                generation=0,
                revision='v_' + rid,
                state='active',
                created_at=NOW,
                created_by=self.users['bob'],
                modified_at=NOW,
                modified_by=self.users['bob'],
            )
            self.resources[rid] = resource
            data = wire(resource)
            self.conn.execute(
                'INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    rid,
                    kind,
                    rid,
                    parent,
                    resource.owner,
                    resource.group,
                    resource.mode,
                    0,
                    resource.revision,
                    'active',
                    data['created_at'],
                    data['modified_at'],
                    canonical(resource).decode(),
                ),
            )
        self.subjects = {}
        self.credentials = {}
        for name, signer in self.signers.items():
            subject = Subject(
                resource_id=self.users[name],
                kind='system' if name in {'root', 'online'} else 'registered',
                primary_group='g_test',
                auth_version=1,
                local_only=name == 'root',
            )
            credential = Credential(
                id=self.keys[name],
                subject_id=subject.resource_id,
                kind='signing_key',
                verifier=signer.public_key,
                ceiling=self.primary,
                not_before=NOW - timedelta(days=7),
                expires_at=NOW + timedelta(days=40),
                revoked_at=None,
            )
            self.subjects[name], self.credentials[name] = subject, credential
            self.conn.execute(
                'INSERT INTO identities VALUES (?,?,?,?)',
                (subject.resource_id, 'subject', 0, canonical(subject).decode()),
            )
            self.conn.execute(
                'INSERT INTO credentials VALUES (?,?,?)',
                (credential.id, subject.resource_id, canonical(credential).decode()),
            )
        self.token = bytes(range(32))
        import hashlib

        self.credentials['token'] = Credential(
            id='t_fixture',
            subject_id=self.users['alice'],
            kind='token',
            verifier=hashlib.sha256(self.token).digest(),
            ceiling=self.primary,
            not_before=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
            revoked_at=None,
        )
        self.conn.execute(
            'INSERT INTO credentials VALUES (?,?,?)',
            ('t_fixture', self.users['alice'], canonical(self.credentials['token']).decode()),
        )
        self.certs = {}
        root_policy = IssuancePolicy(
            issue_grants=self.primary,
            max_cert_ttl_seconds=15 * 86400,
            max_child_ca_depth=3,
            max_delegation_depth=4,
        )
        ca_policy = replace(root_policy, max_cert_ttl_seconds=7 * 86400, max_child_ca_depth=2)
        issue = (self.grants['cert.issue'], self.grants['cert.ca.issue'])
        self.add_cert(
            'c_root',
            'root',
            'root',
            None,
            kind='ca',
            grants=issue,
            issuance=root_policy,
            before=NOW - timedelta(days=5),
            expires=NOW + timedelta(days=30),
        )
        self.add_cert(
            'c_ca',
            'ca',
            'root',
            'c_root',
            kind='ca',
            grants=issue,
            issuance=ca_policy,
            before=NOW - timedelta(days=2),
            expires=NOW + timedelta(days=10),
        )
        leaf = self.grant('resource.basic', {READ + '@1'}, scope='r_area', descendants=True)
        self.add_cert('c_leaf', 'alice', 'ca', 'c_ca', grants=(leaf,))
        self.add_cert(
            'c_delegation',
            'alice',
            'ca',
            'c_ca',
            kind='delegation',
            grants=(leaf,),
            sources=(ResourceRef(id='r_delegation', revision='v_r_delegation'),),
        )
        self.fact = {
            'grantor': self.users['bob'],
            'grantee': self.users['alice'],
            'grants': wire((leaf,)),
            'expires_at': wire(NOW + timedelta(hours=2)),
            'parent_certificate': None,
            'grantor_credential_id': self.keys['bob'],
        }
        self.setting('delegation:r_delegation', self.fact)
        online_policy = IssuancePolicy(
            issue_grants=(self.grants['resource.basic'],),
            max_cert_ttl_seconds=86400,
            max_child_ca_depth=0,
            max_delegation_depth=1,
        )
        self.add_cert(
            'c_online',
            'online',
            'root',
            'c_root',
            kind='ca',
            grants=(self.grants['cert.issue'],),
            issuance=online_policy,
        )
        self.add_cert('c_online_leaf', 'alice', 'online', 'c_online', grants=(leaf,))
        self.add_cert('c_network', 'alice', 'ca', 'c_ca', grants=(self.grants['tool.use'],))
        self.validator = CertificateValidator(
            self.registry,
            self.certs['c_root'],
            self.signers['root'].public_key,
            SERVICE,
            lambda: self.now,
        )
        self.auth = AuthenticationService(
            self.registry,
            self.validator,
            SERVICE,
            lambda: self.now,
            lambda: self.primary,
            lambda: self.primary,
        )
        self.oauth = OAuthConfig(
            enabled=True, clients=(OAuthClient('fixture-client', 'Fixture', scopes=SCOPES),)
        )
        self.auth.oauth_config = self.oauth

    def grant(self, name, operations, *, scope='r_root', descendants=True, constraints=None):
        return CapabilityGrant(
            capability=name,
            version=1,
            scope=Scope(resource_id=scope, descendants=descendants),
            operations=frozenset(operations),
            constraints=constraints or {},
        )

    def add_cert(
        self,
        id,
        subject,
        issuer,
        parent,
        *,
        kind='identity',
        grants=(),
        issuance=None,
        sources=(),
        before=None,
        expires=None,
    ):
        cert = Certificate(
            resource_id=id,
            serial=id,
            subject_id=self.users[subject],
            key_id=self.keys[subject],
            issuer_id=self.users[issuer],
            parent_certificate_id=parent,
            authority_sources=sources,
            kind=kind,
            grants=grants,
            not_before=before or NOW - timedelta(hours=1),
            expires_at=expires or NOW + timedelta(hours=1),
            target_service=SERVICE,
            delegation_depth=0,
            issuance=issuance,
            signature=self.signers[issuer].sign(b'', purpose='certificate'),
        )
        cert = sign_certificate(cert, self.signers[issuer])
        self.certs[id] = cert
        self.conn.execute(
            'INSERT INTO certificates VALUES (?,?,?,?,?)',
            (id, cert.subject_id, parent, 0, canonical(cert).decode()),
        )
        return cert

    def context(self):
        return {
            'root': wire(self.certs['c_root']),
            'root_public': b64(self.signers['root'].public_key),
            'service': SERVICE,
            'capabilities': wire(self.registry.capabilities()),
            'primary_ceiling': wire(self.primary),
            'temporary_ceiling': wire(self.primary),
            'oauth': wire(self.oauth),
        }

    def setting(self, key, value):
        self.conn.execute(
            'INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, canonical(value).decode()),
        )

    @contextmanager
    def setting_change(self, key, value):
        old = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        self.setting(key, value)
        try:
            yield
        finally:
            if old is None:
                self.conn.execute('DELETE FROM settings WHERE key=?', (key,))
            else:
                self.conn.execute('UPDATE settings SET value=? WHERE key=?', (old[0], key))

    @contextmanager
    def body_change(self, table, id, body):
        assert table in {'resources', 'credentials', 'certificates', 'identities'}
        old = self.conn.execute(f'SELECT body FROM {table} WHERE id=?', (id,)).fetchone()[0]
        self.conn.execute(f'UPDATE {table} SET body=? WHERE id=?', (canonical(body).decode(), id))
        try:
            yield
        finally:
            self.conn.execute(f'UPDATE {table} SET body=? WHERE id=?', (old, id))

    def spec(
        self,
        name=READ,
        *,
        version=1,
        effect='read',
        signature=False,
        entries=('network',),
        anonymous=False,
    ):
        spec = OperationSpec(
            name=name,
            version=version,
            input_schema=ResourceRef(id='input'),
            output_schema=ResourceRef(id='output'),
            effect=effect,
            entries=frozenset(entries),
            require_signature=signature,
            anonymous_only=anonymous,
            handler=unused,
            requirements=unused,
        )
        self.registry._operations[(name, version)] = spec
        return {
            key: wire(getattr(spec, key))
            for key in (
                'name',
                'version',
                'effect',
                'entries',
                'require_signature',
                'anonymous_only',
            )
        }

    def request(
        self,
        *,
        name=READ,
        version=1,
        actor='alice',
        subject='default',
        token=False,
        anonymous=False,
        expires=60,
        certificates=(),
        arguments=None,
        request_id='fixture-request',
        resign=True,
        **changes,
    ):
        subject = self.users[actor] if subject == 'default' else subject
        fields = {
            'request_id': request_id,
            'protocol_version': 1,
            'operation': name,
            'contract_version': version,
            'target_service': SERVICE,
            'subject': None if anonymous else subject,
            'arguments': arguments or {'id': 'r_file'},
            'expected_generations': [],
            'expires_at': None if expires is None else wire(NOW + timedelta(seconds=expires)),
            'payload_digest': 'sha256:' + '0' * 64,
            'proof': None,
            'source': 'mcp',
            'return_fields': [],
        }
        fields.update(changes)
        request = decode_packet(canonical(fields))
        request = replace(request, payload_digest=digest(payload_fields(request)))
        if not anonymous:
            proof = (
                TokenProof(credential_id='t_fixture', token=self.token)
                if token
                else SignatureProof(
                    signature=self.signers[actor].sign(signing_bytes(request), purpose='request'),
                    certificates=certificates,
                )
            )
            request = replace(request, proof=proof)
        if not resign:
            request = replace(request, target_service='https://wrong.test.invalid')
        return request

    async def close(self):
        self.conn.close()
        await self.metadata.close()


class Check:
    def __init__(self, binary, fixture):
        self.fixture = fixture
        self.process = subprocess.Popen(
            [str(binary), str(fixture.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
        )
        self.groups = Counter()
        self.failures = []
        self.send(fixture.context())
        assert self.receive() == {'ok': True, 'data': {'ready': True}}

    def send(self, value):
        self.process.stdin.write(
            json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'
        )
        self.process.stdin.flush()

    def receive(self):
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError('native helper ended: ' + self.process.stderr.read()[-1000:])
        return json.loads(line)

    def native(self, command):
        self.send(command)
        return self.receive()

    def compare(self, group, actual, expected, *, count=1):
        self.groups[group] += count
        if actual != expected:
            # Synthetic keys only, but reports still omit request/proof material.
            self.failures.append({
                'group': group,
                'index': self.groups[group],
                'actual': actual,
                'expected': expected,
            })

    async def auth_case(
        self,
        group,
        request,
        policy,
        *,
        entry='network',
        replay=False,
        resource=None,
    ):
        command = {
            'action': 'authenticate',
            'raw': canonical(request).decode(),
            'policy': policy,
            'entry': entry,
            'now': wire(self.fixture.now),
            'resource': resource,
        }
        async with self.fixture.metadata.transaction(write=False) as tx:
            try:
                with resource_execution(resource):
                    principal = await self.fixture.auth.authenticate(request, tx, entry=entry)
                if replay:
                    previous = await tx.request_result(
                        principal.subject, request.request_id, request.payload_digest
                    )
                    assert previous is not None
                    data = {'kind': 'replay', 'result': wire(previous)}
                else:
                    data = {'kind': 'fresh', 'principal': wire(principal)}
                expected = {'ok': True, 'data': data}
            except Failure as exc:
                expected = {'ok': False, 'code': exc.code}
        self.compare(group, self.native(command), expected)

    async def cert_case(self, group, id='c_leaf'):
        async with self.fixture.metadata.transaction(write=False) as tx:
            try:
                cert = await self.fixture.validator.validate(id, tx)
                expected = {
                    'ok': True,
                    'data': {
                        'canonical': canonical(cert).decode(),
                        'signing_digest': digest(canonical(certificate_body(cert))),
                    },
                }
            except Failure as exc:
                expected = {'ok': False, 'code': exc.code}
        self.compare(
            group,
            self.native({'action': 'certificate', 'id': id, 'now': wire(self.fixture.now)}),
            expected,
        )

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=15)
        assert self.process.returncode == 0, self.process.stderr.read()
        self.process.stdout.close()
        self.process.stderr.close()


def ok(data):
    return {'ok': True, 'data': data}


async def run(binary, report):
    with tempfile.TemporaryDirectory(prefix='msg-native-authority-') as directory:
        fixture = Fixture(Path(directory) / 'metadata.db')
        check = Check(binary, fixture)
        try:
            await cases(fixture, check)
        finally:
            check.close()
            await fixture.close()
        summary = {
            'cases': sum(check.groups.values()),
            'groups': dict(sorted(check.groups.items())),
            'failures': check.failures,
            'failure_count': len(check.failures),
            'fixture': 'real Python SqliteMetadataStore schema; deterministic public test keys',
            'limitations': [
                'OAuth issuance, refresh writes and token delivery are not part of this read-only slice',
                'Replay admission returns only the stored result, not a fresh principal',
                'Mode corpus counts each mode/class/permission decision separately',
            ],
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({key: value for key, value in summary.items() if key != 'failures'}))
        for failure in check.failures[:10]:
            print(json.dumps(failure, ensure_ascii=False))
        if check.failures:
            raise AssertionError(f'{len(check.failures)} native authority mismatches')


async def cases(f, check):
    # Exhaustive mode decisions: 4096 modes x 3 classes x 5 permissions.
    decisions = []
    resource = f.resources['r_file']
    for mode in range(0o10000):
        for subject in (resource.owner, 'member', None):
            for permission in ('read', 'write', 'execute', 'list', 'traverse'):
                decisions.append(
                    allows(replace(resource, mode=mode), subject, {resource.group}, permission)
                )
    check.compare(
        'mode_decisions',
        check.native({'action': 'modes'}),
        ok({'count': len(decisions), 'digest': digest(decisions)}),
        count=len(decisions),
    )

    for kind, values in [
        ('subject', f.subjects.values()),
        ('credential', f.credentials.values()),
        ('certificate', f.certs.values()),
        ('resource', f.resources.values()),
    ]:
        for record in values:
            id = getattr(record, 'id', None) or record.resource_id
            check.compare(
                'record_bytes',
                check.native({'action': 'record_digest', 'kind': kind, 'id': id}),
                ok({'digest': digest(record)}),
            )

    for parent in ('r_root', 'r_area', 'r_file', 'r_other'):
        for child in ('r_root', 'r_area', 'r_file', 'r_other'):
            for descendants in (False, True):
                scope = Scope(resource_id=parent, descendants=descendants)
                async with f.metadata.transaction(write=False) as tx:
                    contains = await scope_contains(scope, child, tx)
                check.compare(
                    'scope_contains',
                    check.native({'action': 'scope', 'scope': wire(scope), 'id': child}),
                    ok({'contains': contains}),
                )
    for child_scope in ('r_area', 'r_file', 'r_other'):
        for operations in ({READ + '@1'}, {WRITE + '@1'}, {READ + '@1', WRITE + '@1'}):
            child = f.grant('resource.basic', operations, scope=child_scope)
            parent = f.grant('resource.basic', {READ + '@1'}, scope='r_area')
            async with f.metadata.transaction(write=False) as tx:
                subset = child.operations <= parent.operations and await scope_subset(
                    child.scope, parent.scope, tx
                )
            check.compare(
                'grant_subset',
                check.native({'action': 'subset', 'child': wire(child), 'parent': wire(parent)}),
                ok({'subset': subset}),
            )
    parent = f.grants['tool.use']
    for constraints in (
        {},
        dict(parent.constraints),
        {**parent.constraints, 'methods': ['GET']},
        {**parent.constraints, 'methods': ['POST']},
        {**parent.constraints, 'timeout_ms': 5001},
        {**parent.constraints, 'max_response_bytes': 4095},
        {**parent.constraints, 'ports': [80, 443]},
        {**parent.constraints, 'hosts': []},
        {**parent.constraints, 'max_redirects': True},
    ):
        child = replace(parent, constraints=constraints)
        check.compare(
            'constraint_subset',
            check.native({'action': 'subset', 'child': wire(child), 'parent': wire(parent)}),
            ok({'subset': constraints_subset(child.constraints, parent.constraints)}),
        )

    for child_value, parent_value in [
        (-2, -1),
        (-1, -2),
        (-1, 0),
        (0, -1),
        (10**80, 10**80 + 1),
        (10**80 + 1, 10**80),
        (2, 2.0),
        (2.0, 2),
    ]:
        child = replace(parent, constraints={'max_redirects': child_value})
        upper = replace(parent, constraints={'max_redirects': parent_value})
        check.compare(
            'numeric_constraint_subset',
            check.native({'action': 'subset', 'child': wire(child), 'parent': wire(upper)}),
            ok({'subset': constraints_subset(child.constraints, upper.constraints)}),
        )
    for ports in ([443.0], [443], [444.0]):
        child = replace(parent, constraints={'ports': ports})
        upper = replace(parent, constraints={'ports': [443]})
        check.compare(
            'numeric_allowlist_subset',
            check.native({'action': 'subset', 'child': wire(child), 'parent': wire(upper)}),
            ok({'subset': constraints_subset(child.constraints, upper.constraints)}),
        )
    network = f.certs['c_network']
    for constraints in [
        {**parent.constraints, 'ports': [443.0]},
        {**parent.constraints, 'ports': [0]},
        {**parent.constraints, 'ports': [65536]},
        {**parent.constraints, 'ports': [True]},
        {**parent.constraints, 'hosts': 'not-a-list'},
        {**parent.constraints, 'ports': 'not-a-list'},
        {**parent.constraints, 'max_redirects': -1},
        {**parent.constraints, 'timeout_ms': 0},
        {**parent.constraints, 'timeout_ms': 5000.0},
    ]:
        proposed = sign_certificate(
            replace(network, grants=(replace(parent, constraints=constraints),)), f.signers['ca']
        )
        with f.body_change('certificates', network.resource_id, proposed):
            await check.cert_case('network_constraint_validation', network.resource_id)

    policy = f.spec()
    request = f.request()
    for cert in f.certs:
        await check.cert_case('valid_chain', cert)
    for ids in ((), ('c_leaf',), ('c_leaf', 'c_delegation'), ('c_missing',)):
        await check.auth_case('signed_auth', f.request(certificates=ids), policy)
    for expiry in (None, -300, -1, 0, 0.000001, 1, 299, 300, 300.000001, 301, 600):
        for token in (False, True):
            await check.auth_case('request_expiry', f.request(expires=expiry, token=token), policy)
        await check.auth_case('anonymous_read', f.request(expires=expiry, anonymous=True), policy)
    await check.auth_case('wrong_service', f.request(resign=False), policy)
    await check.auth_case('digest_tamper', replace(request, arguments={'id': 'r_other'}), policy)
    await check.auth_case('signature_tamper', replace(request, source='manual'), policy)
    await check.auth_case('missing_proof', replace(request, proof=None), policy)
    await check.auth_case('subject_spoof', f.request(subject=f.users['bob']), policy)
    await check.auth_case(
        'delegated_subject',
        f.request(subject=f.users['bob'], certificates=('c_delegation',)),
        policy,
    )
    await check.auth_case('entry_denied', request, policy, entry='worker')
    await check.auth_case('root_network', f.request(actor='root'), policy)
    local_policy = f.spec(entries=('network', 'local_admin'))
    await check.auth_case('root_local', f.request(actor='root'), local_policy, entry='local_admin')
    policy = f.spec()
    for name, effect, version, anonymous in [
        ('content.post_edit', 'transaction', 1, False),
        ('identity.link_claim', 'transaction', 1, True),
        ('communication.internet_receive', 'transaction', 1, True),
        ('identity.link_claim', 'transaction', 2, True),
        ('identity.link_claim', 'external', 1, True),
    ]:
        p = f.spec(name, effect=effect, version=version, anonymous=anonymous)
        await check.auth_case(
            'anonymous_write', f.request(name=name, version=version, anonymous=True), p
        )
    policy = f.spec()

    for flag in (False, None, {}, True):
        with f.setting_change('recovery_quarantine', flag):
            await check.auth_case('quarantine_presence', request, policy)
            p = f.spec(WRITE, effect='transaction')
            await check.auth_case('quarantine_write', f.request(name=WRITE), p)
    for key, value in [
        ('active_root_certificate', 'other-root'),
        ('identity_archived:' + f.users['alice'], True),
        ('identity_archived:' + f.users['alice'], False),
    ]:
        with f.setting_change(key, value):
            await check.auth_case('authority_fences', request, policy)

    for name, field, value in [
        ('alice', 'revoked_at', NOW),
        ('alice', 'expires_at', NOW),
        ('alice', 'not_before', NOW + timedelta(microseconds=1)),
        ('alice', 'expires_at', None),
        ('alice', 'ceiling', ()),
        ('alice', 'kind', 'ssh_key'),
        ('token', 'verifier', b'wrong'),
        ('token', 'revoked_at', NOW),
        ('token', 'expires_at', NOW),
    ]:
        credential = f.credentials[name]
        with f.body_change('credentials', credential.id, replace(credential, **{field: value})):
            await check.auth_case('credential_state', f.request(token=name == 'token'), policy)
    p = f.spec(signature=True)
    await check.auth_case('signature_required', f.request(token=True), p)
    policy = f.spec()
    derived = replace(f.credentials['token'], source_credential_id=f.keys['alice'])
    with f.body_change('credentials', derived.id, derived):
        await check.auth_case('missing_token_binding', f.request(token=True), policy)
    source = {
        'parent': f.keys['alice'],
        'subject': f.users['alice'],
        'auth_version': 1,
        'ceiling': wire(f.primary),
    }
    with f.body_change('credentials', derived.id, derived):
        for binding in ('browser:', 'api:'):
            f.conn.execute(
                'INSERT INTO oauth_states VALUES (?,?,?,?)',
                (
                    binding + derived.id,
                    'binding',
                    wire(NOW + timedelta(days=1)),
                    canonical(source).decode(),
                ),
            )
            await check.auth_case('oauth_bound_token', f.request(token=True), policy)
            for field, value in [('revoked_at', NOW), ('expires_at', NOW), ('ceiling', ())]:
                parent = f.credentials['alice']
                with f.body_change('credentials', parent.id, replace(parent, **{field: value})):
                    await check.auth_case('oauth_live_parent', f.request(token=True), policy)
            with f.body_change(
                'identities', f.users['alice'], replace(f.subjects['alice'], auth_version=2)
            ):
                await check.auth_case('oauth_auth_version', f.request(token=True), policy)
            nested = {**source, 'session': 'session:fixture'}
            f.conn.execute(
                'UPDATE oauth_states SET body=? WHERE id=?',
                (canonical(nested).decode(), binding + derived.id),
            )
            f.conn.execute(
                'INSERT INTO oauth_states VALUES (?,?,?,?)',
                (
                    'session:fixture',
                    'session',
                    wire(NOW + timedelta(seconds=60)),
                    canonical(source).decode(),
                ),
            )
            await check.auth_case('oauth_session_source', f.request(token=True), policy)
            f.conn.execute(
                'UPDATE oauth_states SET body=? WHERE id=?',
                (canonical({**source, 'revoked': True}).decode(), 'session:fixture'),
            )
            await check.auth_case('oauth_session_revoked', f.request(token=True), policy)
            f.conn.execute('DELETE FROM oauth_states')
        family = {**source, 'client_id': 'fixture-client', 'scopes': ['msg.read', 'msg.write']}
        access = {'family': 'family:fixture'}
        f.conn.execute(
            'INSERT INTO oauth_states VALUES (?,?,?,?)',
            (
                'access:' + derived.id,
                'access',
                wire(NOW + timedelta(days=1)),
                canonical(access).decode(),
            ),
        )
        for field, value in [
            (None, None),
            ('revoked', True),
            ('client_id', 'missing-client'),
            ('scopes', ['unknown.scope']),
            ('ceiling', []),
            ('auth_version', 9),
        ]:
            body = family if field is None else {**family, field: value}
            f.conn.execute(
                'INSERT OR REPLACE INTO oauth_states VALUES (?,?,?,?)',
                (
                    'family:fixture',
                    'family',
                    wire(NOW + timedelta(seconds=60)),
                    canonical(body).decode(),
                ),
            )
            await check.auth_case('oauth_family', f.request(token=True), policy)
        f.conn.execute(
            'UPDATE oauth_states SET expires=? WHERE id=?', (wire(NOW), 'family:fixture')
        )
        await check.auth_case('oauth_family_expired', f.request(token=True), policy)
        mcp_grants = (f.grant('resource.basic', {READ + '@1'}),)
        mcp_token = replace(derived, ceiling=mcp_grants)
        audience = SERVICE + '/-/mcp'
        mcp_family = {
            **source,
            'client_id': 'fixture-client',
            'resource': audience,
            'scopes': ['msg.mcp.read'],
            'ceiling': wire(mcp_grants),
        }
        f.conn.execute(
            'UPDATE oauth_states SET expires=?,body=? WHERE id=?',
            (wire(NOW + timedelta(seconds=60)), canonical(mcp_family).decode(), 'family:fixture'),
        )
        with f.body_change('credentials', derived.id, mcp_token):
            for resource in (None, audience, SERVICE + '/other'):
                await check.auth_case(
                    'mcp_audience', f.request(token=True), policy, resource=resource
                )
            for scopes in (['msg.read', 'msg.mcp.read'], ['msg.mcp.post'], ['msg.mcp.read']):
                f.conn.execute(
                    'UPDATE oauth_states SET body=? WHERE id=?',
                    (canonical({**mcp_family, 'scopes': scopes}).decode(), 'family:fixture'),
                )
                await check.auth_case(
                    'mcp_scope_ceiling', f.request(token=True), policy, resource=audience
                )
        await check.auth_case(
            'mcp_operation_denied', f.request(token=True), policy, resource=audience
        )
        f.conn.execute('DELETE FROM oauth_states')

    for actor, kind in [('alice', 'custodial'), ('alice', 'registered')]:
        subject = f.subjects[actor]
        with f.body_change('identities', subject.resource_id, replace(subject, kind=kind)):
            for name, arguments in [
                (WRITE, {}),
                ('tool.run', {}),
                ('achievement.finish', {}),
                ('transfer.open', {'direction': 'download'}),
                ('transfer.open', {'direction': 'upload'}),
            ]:
                p = f.spec(name, effect='transaction', signature=name == 'achievement.finish')
                await check.auth_case(
                    'custodial_limits', f.request(name=name, token=True, arguments=arguments), p
                )

    for kind, version, nonce_size in [
        ('identity.temporary', 3, 23),
        ('identity.temporary', 3, 24),
        ('identity.custodial_create', 2, 32),
        ('identity.temporary', 3, 64),
        ('identity.temporary', 3, 65),
    ]:
        p = f.spec(kind, version=version, effect='transaction')
        req = f.request(
            name=kind,
            version=version,
            anonymous=True,
            arguments={'nonce': b64(bytes(range(nonce_size)))},
        )
        await check.auth_case('bootstrap', req, p)
    p = f.spec('identity.register', effect='transaction')
    req = f.request(
        name='identity.register', arguments={'public_key': b64(f.signers['alice'].public_key)}
    )
    await check.auth_case('register', req, p)
    with f.setting_change('delegated_identity:' + f.users['alice'], {'locked': True}):
        await check.auth_case('register_delegated_denied', req, p)

    # Invalid child conditions are re-signed to test rules after cryptography.
    leaf = f.certs['c_leaf']
    for field, value in [
        ('target_service', 'https://other.test.invalid'),
        ('not_before', NOW + timedelta(seconds=1)),
        ('expires_at', NOW),
        ('issuer_id', f.users['alice']),
        ('subject_id', 'u_root'),
        ('delegation_depth', 5),
        ('not_before', NOW - timedelta(days=3)),
        ('expires_at', NOW + timedelta(days=11)),
        ('expires_at', NOW + timedelta(days=8)),
        ('issuance', f.certs['c_ca'].issuance),
        ('grants', (f.grant('resource.basic', {'unknown.call@1'}),)),
        ('grants', (f.grant('missing.capability', {READ + '@1'}),)),
        ('grants', (replace(leaf.grants[0], constraints={'unknown': 'x'}),)),
        ('subject_id', f.users['bob']),
    ]:
        changed = sign_certificate(replace(leaf, **{field: value}), f.signers['ca'])
        with f.body_change('certificates', leaf.resource_id, changed):
            await check.cert_case('invalid_child')
    with f.body_change('certificates', 'c_leaf', replace(leaf, serial='tampered')):
        await check.cert_case('invalid_signature')
    with f.body_change('certificates', 'c_root', replace(f.certs['c_root'], serial='tampered')):
        await check.cert_case('anchor_pin')
    for id in ('c_leaf', 'c_ca', 'c_root'):
        f.conn.execute('UPDATE certificates SET revoked=1 WHERE id=?', (id,))
        await check.cert_case('certificate_revoked')
        f.conn.execute('UPDATE certificates SET revoked=0 WHERE id=?', (id,))
    for name in ('alice', 'ca', 'root'):
        old = f.credentials[name]
        with f.body_change('credentials', old.id, replace(old, revoked_at=NOW)):
            await check.cert_case('issuer_key_revoked')
    with f.body_change(
        'certificates', 'c_ca', replace(f.certs['c_ca'], parent_certificate_id='c_leaf')
    ):
        await check.cert_case('certificate_cycle')
    for field, value in [
        ('kind', 'identity'),
        ('issuance', None),
        ('grants', (f.grants['cert.ca.issue'],)),
        ('grants', (f.grants['cert.issue'],)),
        ('issuance', replace(f.certs['c_ca'].issuance, max_child_ca_depth=3)),
        ('issuance', replace(f.certs['c_ca'].issuance, max_cert_ttl_seconds=16 * 86400)),
        ('issuance', replace(f.certs['c_ca'].issuance, max_delegation_depth=5)),
    ]:
        cert = sign_certificate(replace(f.certs['c_ca'], **{field: value}), f.signers['root'])
        with f.body_change('certificates', 'c_ca', cert):
            await check.cert_case('ca_policy')
    for field, value in [('kind', 'capability'), ('kind', 'delegation')]:
        cert = sign_certificate(
            replace(f.certs['c_online_leaf'], **{field: value}), f.signers['online']
        )
        with f.body_change('certificates', 'c_online_leaf', cert):
            await check.cert_case('online_ca_limits', 'c_online_leaf')
    for field, value in [
        ('issuance', replace(f.certs['c_online'].issuance, max_child_ca_depth=1)),
        ('grants', (f.grants['cert.issue'], f.grants['cert.ca.issue'])),
    ]:
        cert = sign_certificate(replace(f.certs['c_online'], **{field: value}), f.signers['root'])
        with f.body_change('certificates', 'c_online', cert):
            await check.cert_case('online_ca_limits', 'c_online_leaf')

    for field, value in [
        ('expires_at', wire(NOW)),
        ('grantee', f.users['bob']),
        ('grantor', f.users['alice']),
        ('grants', []),
        ('parent_certificate', 'c_delegation'),
    ]:
        with f.setting_change('delegation:r_delegation', {**f.fact, field: value}):
            await check.cert_case('delegation_source', 'c_delegation')
    resource = f.resources['r_delegation']
    for field, value in [('state', 'archived'), ('revision', 'v_changed')]:
        with f.body_change('resources', resource.id, replace(resource, **{field: value})):
            await check.cert_case('delegation_resource', 'c_delegation')
    credential = f.credentials['bob']
    for field, value in [('revoked_at', NOW), ('ceiling', ())]:
        with f.body_change('credentials', credential.id, replace(credential, **{field: value})):
            await check.cert_case('delegation_live_source', 'c_delegation')
    with f.setting_change('identity_archived:' + f.users['bob'], True):
        await check.cert_case('delegation_live_source', 'c_delegation')

    # CSR and publication signed bytes use the same actual Python model.
    csr = CertificateRequest(
        resource_id='csr_new',
        applicant=f.users['alice'],
        subject_id=f.users['alice'],
        requested_issuer=f.users['ca'],
        public_key=f.signers['alice'].public_key,
        kind='identity',
        grants=leaf.grants,
        issuance=None,
        requested_ttl_seconds=7200,
        target_service=SERVICE,
        delegation_depth=0,
        authority_sources=(),
        request_digest='',
        possession_proof=f.signers['alice'].sign(b'', purpose='csr'),
    )
    csr = replace(
        csr,
        request_digest=digest(csr_body(csr)),
        possession_proof=f.signers['alice'].sign(canonical(csr_body(csr)), purpose='csr'),
    )
    proposed = sign_certificate(replace(leaf, resource_id='c_new', serial='new'), f.signers['ca'])
    for field, val in [
        (None, None),
        ('request_digest', 'sha256:' + '0' * 64),
        ('requested_issuer', f.users['bob']),
    ]:
        candidate = csr if field is None else replace(csr, **{field: val})
        async with f.metadata.transaction(write=False) as tx:
            try:
                result = await f.validator.validate_publication(proposed, candidate, tx)
                expected = ok({'canonical': canonical(result).decode()})
            except Failure as exc:
                expected = {'ok': False, 'code': exc.code}
        check.compare(
            'csr_publication',
            check.native({
                'action': 'publication',
                'certificate': wire(proposed),
                'csr': wire(candidate),
                'now': wire(f.now),
            }),
            expected,
        )

    # Rotation retries may read the exact saved result, never get a new principal.
    policy = f.spec('identity.token_rotate', version=2, effect='transaction')
    req = f.request(
        name='identity.token_rotate', version=2, token=True, request_id='rotation-fixed'
    )
    result = OperationResult(
        request_id=req.request_id,
        operation='identity.token_rotate',
        status='ok',
        actor=f.users['alice'],
        subject=f.users['alice'],
        committed_at=NOW,
        data={'previous_credential': 't_fixture', 'credential_id': 't_next'},
    )
    successor = replace(f.credentials['token'], id='t_next')
    f.conn.execute(
        'INSERT INTO credentials VALUES (?,?,?)',
        ('t_next', f.users['alice'], canonical(successor).decode()),
    )
    f.conn.execute(
        'INSERT INTO results VALUES (?,?,?,?)',
        (f.users['alice'], req.request_id, req.payload_digest, canonical(result).decode()),
    )
    with f.body_change('credentials', 't_fixture', replace(f.credentials['token'], revoked_at=NOW)):
        await check.auth_case('rotation_exact_replay', req, policy, replay=True)
        await check.auth_case(
            'rotation_new_request_denied',
            f.request(name='identity.token_rotate', version=2, token=True, request_id='other'),
            policy,
        )
        changed = f.request(
            name='identity.token_rotate',
            version=2,
            token=True,
            request_id=req.request_id,
            arguments={'new': 'content'},
        )
        await check.auth_case('rotation_digest_conflict', changed, policy)
        with f.body_change('credentials', 't_next', replace(successor, revoked_at=NOW)):
            await check.auth_case('rotation_dead_successor', req, policy)
    check.compare(
        'ledger_lookup',
        check.native({
            'action': 'result',
            'subject': f.users['alice'],
            'id': req.request_id,
            'digest': req.payload_digest,
        }),
        ok({'result': wire(result)}),
    )
    check.compare(
        'ledger_subject_isolation',
        check.native({
            'action': 'result',
            'subject': f.users['bob'],
            'id': req.request_id,
            'digest': req.payload_digest,
        }),
        ok({'result': None}),
    )
    check.compare(
        'ledger_anonymous_isolation',
        check.native({
            'action': 'result',
            'subject': None,
            'id': req.request_id,
            'digest': req.payload_digest,
        }),
        ok({'result': None}),
    )

    recovery_secret = b64(bytes(range(32, 64)))
    recovery_args = {
        'credential_id': 't_fixture',
        'recovery_secret': recovery_secret,
        'original_request_id': 'original-issue',
    }
    recover = f.request(
        name='identity.token_recover', arguments=recovery_args, request_id='recovery-fixed'
    )
    recover = replace(recover, proof=None)
    policy = f.spec('identity.token_recover', effect='transaction')
    f.conn.execute(
        'INSERT INTO token_deliveries VALUES (?,?,?,?,?,?,?,?)',
        (
            't_fixture',
            f.users['alice'],
            'original-issue',
            'unused',
            recovery_verifier(recovery_secret),
            wire(NOW + timedelta(seconds=60)),
            None,
            None,
        ),
    )
    await check.auth_case('token_recovery', recover, policy)
    for key, value in [
        ('recovery_secret', b64(bytes(32))),
        ('original_request_id', 'other'),
        ('recovery_secret', b64(bytes(31))),
    ]:
        bad = f.request(name='identity.token_recover', arguments={**recovery_args, key: value})
        await check.auth_case('token_recovery_binding', replace(bad, proof=None), policy)
    for field, value in [('recovery_expires_at', wire(NOW)), ('consumed_at', wire(NOW))]:
        old = f.conn.execute(f'SELECT {field} FROM token_deliveries').fetchone()[0]
        f.conn.execute(f'UPDATE token_deliveries SET {field}=?', (value,))
        await check.auth_case('token_recovery_state', recover, policy)
        f.conn.execute(f'UPDATE token_deliveries SET {field}=?', (old,))
    previous = replace(result, request_id=recover.request_id, operation=recover.operation)
    f.conn.execute(
        'INSERT INTO results VALUES (?,?,?,?)',
        (
            f.users['alice'],
            recover.request_id,
            recover.payload_digest,
            canonical(previous).decode(),
        ),
    )
    f.conn.execute('UPDATE token_deliveries SET consumed_at=?', (wire(NOW),))
    with f.body_change('credentials', 't_fixture', replace(f.credentials['token'], revoked_at=NOW)):
        await check.auth_case('token_recovery_replay', recover, policy, replay=True)
    f.conn.execute('DELETE FROM token_deliveries')

    # WAL snapshots cannot combine old credential rows with new revocation facts.
    f.setting('snapshot_probe', 'old')
    check.send({'action': 'hold_snapshot'})
    check.compare('snapshot_before', check.receive(), ok({'before': 'old'}))
    f.setting('snapshot_probe', 'new')
    check.send({'resume': True})
    check.compare('snapshot_stable', check.receive(), ok({'after': 'old'}))
    check.compare(
        'snapshot_fresh',
        check.native({'action': 'setting', 'key': 'snapshot_probe'}),
        ok({'exists': True, 'value': 'new'}),
    )
    f.conn.execute('UPDATE resources SET parent=? WHERE id=?', ('r_root', 'r_root'))
    # Body parent links, not the incidental SQL column, are the Python authority view.
    with f.body_change('resources', 'r_area', replace(f.resources['r_area'], parent='r_file')):
        command = {
            'action': 'scope',
            'scope': wire(Scope(resource_id='r_other', descendants=True)),
            'id': 'r_file',
        }
        check.compare('parent_cycle', check.native(command), {'ok': False, 'code': 'parent_cycle'})
    f.conn.execute('UPDATE resources SET parent=NULL WHERE id=?', ('r_root',))

    # Runtime generation mismatch remains latched even when the old value returns.
    policy = f.spec()
    with f.setting_change('recovery_runtime_generation', 'after-restore'):
        await check.auth_case('runtime_stale', request, policy)
    await check.auth_case('runtime_stale_latched', request, policy)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.binary.resolve(), args.report))
