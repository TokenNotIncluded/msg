"""Native issuance and browser sessions against a Python-created PG schema.

Only the existing local disposable-database fixture is used. All keys, tokens,
and signatures in this harness are synthetic public test material.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from postgres_fixture import database, replace_fixture_storage
from test_identity_parity import NOW, SERVICE, Fixture

from msg.core.codec import b64, canonical, decode, digest, unb64, wire
from msg.core.models import Certificate, CertificateRequest, Resource, Signature
from msg.core.registry import Registry
from msg.core.requests import payload_fields
from msg.security.age_keys import _encode, recipient_from_identity
from msg.security.certificates import CertificateValidator, csr_body, sign_certificate, verify_csr
from msg.security.crypto import Ed25519Signer, key_id, subject_id, verify


class Native:
    def __init__(self, binary, context):
        self.process = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            env=os.environ.copy(),
        )
        self.send(context)
        assert self.read() == {'ok': True, 'data': {'ready': True}}
        self.checks = 0

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def read(self):
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError('native helper exited: ' + self.process.stderr.read())
        return json.loads(line)

    def call(self, action, *, error=None, **values):
        self.send({'action': action, 'now': wire(NOW), **values})
        result = self.read()
        self.checks += 1
        if error:
            assert result == {'ok': False, 'code': error}, result
            return None
        assert result['ok'], result
        return result['data']

    def handle(self, request, **values):
        return self.call('handle', request=wire(request), **values)

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=15)
        assert self.process.returncode == 0, self.process.stderr.read()


def configure(fixture):
    definitions = [
        ('identity.register', 2, True, 'write'),
        ('identity.token_create', 3, True, 'write'),
        ('identity.token_rotate', 2, False, 'write'),
        ('identity.token_recover', 1, False, 'write'),
        ('identity.key_revoke', 1, True, 'write'),
        ('identity.key_add', 1, True, 'write'),
        ('identity.identity_key_get', 1, False, 'read'),
        ('identity.encryption_key_get', 1, False, 'read'),
        ('identity.custodial_create', 2, False, 'write'),
        ('identity.custodial_status', 1, False, 'read'),
        ('identity.session_approve', 1, True, 'write'),
        ('cert.request', 1, True, 'write'),
        ('cert.get', 1, False, 'read'),
        ('cert.cancel', 1, True, 'write'),
        ('cert.publish', 1, True, 'write'),
        ('cert.revoke', 1, True, 'write'),
        ('content.post_get', 1, False, 'read'),
        ('content.post_create', 1, False, 'write'),
    ]
    operations = [
        fixture.spec(
            name,
            version=version,
            signature=signature,
            effect='transaction' if effect == 'write' else effect,
        )
        for name, version, signature, effect in definitions
    ]
    operation_ids = frozenset(f'{name}@{version}' for name, version, _, _ in definitions)
    for family in ('resource.basic', 'identity.basic'):
        fixture.grants[family] = replace(fixture.grants[family], operations=operation_ids)
    fixture.primary = tuple(fixture.grants.values())
    registry = Registry()
    for capability in fixture.registry.capabilities():
        if capability.name in {'resource.basic', 'identity.basic'}:
            capability = replace(capability, operations=operation_ids)
        registry.add_capability(capability)
    fixture.registry = registry
    root = fixture.certs['c_root']
    root = replace(root, issuance=replace(root.issuance, issue_grants=fixture.primary))
    fixture.certs['c_root'] = sign_certificate(root, fixture.signers['root'])
    online = fixture.certs['c_online']
    online = replace(
        online,
        issuance=replace(
            online.issuance,
            issue_grants=tuple(fixture.grants[k] for k in ('resource.basic', 'identity.basic')),
        ),
    )
    fixture.certs['c_online'] = sign_certificate(online, fixture.signers['root'])
    ca = fixture.certs['c_ca']
    fixture.certs['c_ca'] = sign_certificate(
        replace(ca, issuance=replace(ca.issuance, issue_grants=fixture.primary)),
        fixture.signers['root'],
    )
    for certificate in ('c_root', 'c_online', 'c_ca'):
        fixture.conn.execute(
            'UPDATE certificates SET body=? WHERE id=?',
            (canonical(fixture.certs[certificate]).decode(), certificate),
        )
    fixture.setting('online_ca_certificate', 'c_online')
    for rid in ('t_certificates', 't_wiki', 't_csr', fixture.users['ca']):
        kind = 'user' if rid == fixture.users['ca'] else 'topic'
        resource = Resource(
            id=rid,
            type=kind,
            type_version=1,
            name=rid,
            parent='r_root',
            owner='u_root',
            group='g_public',
            mode=0o755,
            generation=0,
            revision=None,
            state='active',
            created_at=NOW,
            created_by='u_root',
            modified_at=NOW,
            modified_by='u_root',
        )
        fixture.conn.execute(
            'INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                rid,
                kind,
                rid,
                'r_root',
                'u_root',
                'g_public',
                0o755,
                0,
                None,
                'active',
                wire(NOW),
                wire(NOW),
                canonical(resource).decode(),
            ),
        )
    for name, seed in [('new', 42), ('rollback', 43)]:
        fixture.signers[name] = Ed25519Signer.from_bytes(bytes([seed]) * 32)
        fixture.users[name] = subject_id(fixture.signers[name].public_key)
        fixture.keys[name] = key_id(fixture.signers[name].public_key)
    fixture.validator = CertificateValidator(
        registry, fixture.certs['c_root'], fixture.signers['root'].public_key, SERVICE, lambda: NOW
    )
    context = fixture.context()
    context['operations'] = operations
    context['resource_types'] = [
        {'name': kind, 'version': 1, 'container': container}
        for kind, container in (
            ('space', True),
            ('topic', True),
            ('user', True),
            ('csr', False),
            ('certificate', False),
        )
    ]
    return context


async def exercise(binary):
    with tempfile.TemporaryDirectory(prefix='msg-native-identity-') as folder, database() as dsn:
        fixture = Fixture(Path(folder) / 'fixture.sqlite')
        context = configure(fixture)
        await replace_fixture_storage(fixture, dsn)
        native = Native(binary, context)
        try:
            recipient = recipient_from_identity(_encode('age-secret-key-', bytes([1]) * 32).upper())
            register = fixture.request(
                name='identity.register',
                version=2,
                actor='new',
                request_id='native-register',
                arguments={
                    'handle': 'native-user',
                    'public_key': b64(fixture.signers['new'].public_key),
                    'encryption_recipient': recipient,
                },
            )
            registered = native.handle(register)['data']
            assert registered['subject_id'] == fixture.users['new']
            assert registered['key_id'] == fixture.keys['new']
            async with fixture.metadata.transaction(write=False) as tx:
                certificate = await fixture.validator.validate(registered['certificate_id'], tx)
                assert certificate.subject_id == fixture.users['new']
                assert certificate.key_id == fixture.keys['new']
            replay = native.handle(register)
            assert replay['replay']['data'] == registered

            csr_signer = Ed25519Signer.from_bytes(bytes([44]) * 32)
            csr = CertificateRequest(
                resource_id='unsigned-resource-placeholder',
                applicant=fixture.users['new'],
                subject_id=fixture.users['new'],
                requested_issuer=fixture.users['ca'],
                public_key=csr_signer.public_key,
                kind='identity',
                grants=(
                    fixture.grant(
                        'resource.basic',
                        {'content.post_get@1', 'identity.key_add@1', 'identity.token_create@3'},
                        scope='r_file',
                        descendants=False,
                    ),
                ),
                issuance=None,
                requested_ttl_seconds=3600,
                target_service=SERVICE,
                delegation_depth=0,
                authority_sources=(),
                request_digest='',
                possession_proof=Signature(
                    key_id=csr_signer.key_id, algorithm='ed25519', value=b''
                ),
            )
            csr = replace(
                csr,
                request_digest=digest(csr_body(csr)),
                possession_proof=csr_signer.sign(canonical(csr_body(csr)), purpose='csr'),
            )
            create_csr = fixture.request(
                name='cert.request',
                actor='new',
                request_id='native-csr',
                arguments={
                    'requested_issuer': csr.requested_issuer,
                    'public_key': b64(csr.public_key),
                    'kind': csr.kind,
                    'grants': wire(csr.grants),
                    'requested_ttl_seconds': csr.requested_ttl_seconds,
                    'possession_proof': wire(csr.possession_proof),
                },
            )
            created_csr = native.handle(create_csr)['data']
            got_csr = native.handle(
                fixture.request(
                    name='cert.get',
                    actor='new',
                    request_id='native-csr-get',
                    arguments={'id': created_csr['csr_id']},
                )
            )['data']
            verify_csr(decode(CertificateRequest, got_csr['request']))
            assert got_csr['state']['status'] == 'pending'
            cert = Certificate(
                resource_id='cert_native_published',
                serial='serial_native_published',
                subject_id=fixture.users['new'],
                key_id=csr_signer.key_id,
                issuer_id=fixture.users['ca'],
                parent_certificate_id='c_ca',
                authority_sources=(),
                kind='identity',
                grants=csr.grants,
                not_before=NOW,
                expires_at=NOW + timedelta(seconds=3600),
                target_service=SERVICE,
                delegation_depth=0,
                issuance=None,
                signature=Signature(key_id=fixture.keys['ca'], algorithm='ed25519', value=b''),
            )
            cert = sign_certificate(cert, fixture.signers['ca'])
            publish = fixture.request(
                name='cert.publish',
                actor='ca',
                certificates=('c_ca',),
                request_id='native-publish',
                arguments={'csr_id': created_csr['csr_id'], 'certificate': wire(cert)},
            )
            assert native.handle(publish)['data']['status'] == 'issued'
            async with fixture.metadata.transaction(write=False) as tx:
                await fixture.validator.validate(cert.resource_id, tx)
                assert (await tx.credential(csr_signer.key_id)).ceiling == csr.grants
            # A valid signing credential allows these operations only on r_file.
            # The domain handler itself must reject minting keys or tokens.
            fixture.signers['narrow'] = csr_signer
            fixture.users['narrow'] = fixture.users['new']
            fixture.keys['narrow'] = csr_signer.key_id
            added_key = Ed25519Signer.from_bytes(bytes([45]) * 32)
            possession = added_key.sign(
                canonical({
                    'subject_id': fixture.users['new'],
                    'public_key': b64(added_key.public_key),
                }),
                purpose='key-add',
            )
            native.handle(
                fixture.request(
                    name='identity.key_add',
                    actor='narrow',
                    request_id='native-narrow-key',
                    certificates=(cert.resource_id,),
                    arguments={
                        'public_key': b64(added_key.public_key),
                        'ceiling': wire(csr.grants),
                        'possession_proof': wire(possession),
                    },
                ),
                error='credential_ceiling',
            )
            native.handle(
                fixture.request(
                    name='identity.token_create',
                    version=3,
                    actor='narrow',
                    request_id='native-narrow-token',
                    certificates=(cert.resource_id,),
                    arguments={
                        'nonce': b64(bytes([46]) * 32),
                        'recovery_secret': b64(bytes([47]) * 32),
                        'ceiling': wire(csr.grants),
                        'ttl': 3600,
                    },
                ),
                error='credential_ceiling',
            )
            native.handle(
                fixture.request(
                    name='cert.revoke',
                    actor='new',
                    request_id='native-no-revoke',
                    arguments={'id': cert.resource_id, 'reason': 'no authority'},
                ),
                error='certificate_revoke_required',
            )

            # A failure after all domain writes rolls back resources, identity,
            # keys, certificate and certificate audit together.
            failed = fixture.request(
                name='identity.register',
                version=2,
                actor='rollback',
                request_id='native-rollback',
                arguments={
                    'handle': 'rolled-back',
                    'public_key': b64(fixture.signers['rollback'].public_key),
                    'encryption_recipient': recipient_from_identity(
                        _encode('age-secret-key-', bytes([3]) * 32).upper()
                    ),
                },
            )
            tables = (
                'resources',
                'identities',
                'credentials',
                'certificates',
                'audit',
                'identity_keys',
                'encryption_subkeys',
            )
            before = [
                fixture.conn.execute('SELECT count(*) FROM ' + table).fetchone() for table in tables
            ]
            native.handle(failed, fail_after=True, error='fixture_failure')
            after = [
                fixture.conn.execute('SELECT count(*) FROM ' + table).fetchone() for table in tables
            ]
            assert before == after

            create = fixture.request(
                name='identity.token_create',
                version=3,
                actor='new',
                request_id='native-token',
                arguments={
                    'nonce': b64(bytes([7]) * 32),
                    'recovery_secret': b64(bytes([8]) * 32),
                    'ceiling': wire(fixture.primary),
                    'ttl': 3600,
                },
            )
            created = native.handle(create)['data']
            assert 'token' not in created
            delivered = native.call(
                'claim',
                request=wire(create),
                subject=fixture.users['new'],
                credential_id=created['credential_id'],
            )
            native.call(
                'claim',
                request=wire(create),
                subject=fixture.users['new'],
                credential_id=created['credential_id'],
                error='token_delivery_unavailable',
            )
            token = unb64(delivered['token'])
            row = fixture.conn.execute(
                'SELECT body FROM credentials WHERE id=?', (created['credential_id'],)
            ).fetchone()
            credential = json.loads(row[0])
            assert unb64(credential['verifier']) == hashlib.sha256(token).digest()
            assert credential['source_credential_id'] == fixture.keys['new']
            cached = fixture.conn.execute(
                'SELECT body FROM results WHERE subject=? AND request_id=?',
                (fixture.users['new'], 'native-token'),
            ).fetchone()[0]
            assert delivered['token'] not in cached and b64(bytes([8]) * 32) not in cached

            fixture.token = token
            rotate = fixture.request(
                name='identity.token_rotate',
                version=2,
                actor='new',
                token=True,
                token_id=created['credential_id'],
                request_id='native-rotate',
                arguments={
                    'nonce': b64(bytes([10]) * 32),
                    'recovery_secret': b64(bytes([11]) * 32),
                },
            )
            rotated = native.handle(rotate)['data']
            new_credential = json.loads(
                fixture.conn.execute(
                    'SELECT body FROM credentials WHERE id=?', (rotated['credential_id'],)
                ).fetchone()[0]
            )
            assert new_credential['source_credential_id'] == fixture.keys['new']
            assert fixture.conn.execute(
                'SELECT id FROM credential_states WHERE id=?', ('api:' + rotated['credential_id'],)
            ).fetchone()
            old = json.loads(
                fixture.conn.execute(
                    'SELECT body FROM credentials WHERE id=?', (created['credential_id'],)
                ).fetchone()[0]
            )
            assert old['revoked_at'] == wire(NOW)
            assert native.handle(rotate)['replay']['data'] == rotated
            saved_rotation = fixture.conn.execute(
                'SELECT body FROM results WHERE subject=? AND request_id=?',
                (fixture.users['new'], rotate.request_id),
            ).fetchone()[0]
            modified_rotation = json.loads(saved_rotation)
            modified_rotation['data']['token'] = 'old-format-secret-must-not-escape'
            fixture.conn.execute(
                'UPDATE results SET body=? WHERE subject=? AND request_id=?',
                (canonical(modified_rotation).decode(), fixture.users['new'], rotate.request_id),
            )
            assert 'token' not in native.handle(rotate)['replay']['data']
            modified_rotation['resources'] = [{'id': 'r_file'}]
            fixture.conn.execute(
                'UPDATE results SET body=? WHERE subject=? AND request_id=?',
                (canonical(modified_rotation).decode(), fixture.users['new'], rotate.request_id),
            )
            native.handle(rotate, error='replay_result_mismatch')
            fixture.conn.execute(
                'UPDATE results SET body=? WHERE subject=? AND request_id=?',
                (saved_rotation, fixture.users['new'], rotate.request_id),
            )

            pending = native.call('pending')
            pending_state = fixture.conn.execute('SELECT body FROM credential_states').fetchall()
            assert all(
                pending['value'] not in row[0] and pending['user_code'] not in row[0]
                for row in pending_state
            )
            approve = fixture.request(
                name='identity.session_approve',
                actor='new',
                request_id='native-approve',
                arguments={'user_code': pending['user_code'], 'decision': 'approve'},
            )
            native.call('approve', request=wire(approve))
            session = native.call('poll', value=pending['value'])
            native.call('poll', value=pending['value'], error='invalid_grant')
            native.call('session', cookie=session['cookie'])
            native.call('logout', cookie=session['cookie'])
            native.call('session', cookie=session['cookie'], error='invalid_grant')

            custodial = fixture.request(
                name='identity.custodial_create',
                version=2,
                anonymous=True,
                request_id='native-custodial',
                arguments={
                    'handle': 'custody-user',
                    'nonce': b64(bytes([20]) * 32),
                    'recovery_secret': b64(bytes([21]) * 32),
                },
            )
            custody = native.handle(custodial)['data']
            assert custody['server_signable'] and custody['server_decryptable']
            fixture.users['cust'] = custody['subject_id']
            fixture.token = unb64(
                native.call(
                    'claim',
                    request=wire(custodial),
                    subject=custody['subject_id'],
                    credential_id=custody['credential_id'],
                )['token']
            )
            status = fixture.request(
                name='identity.custodial_status',
                actor='cust',
                token=True,
                token_id=custody['credential_id'],
                request_id='native-custody-status',
                arguments={},
            )
            status = replace(status, arguments={})
            status = replace(status, payload_digest=digest(payload_fields(status)))
            native.handle(status)
            # Make a new signed token envelope; changing request_id without its
            # digest would invalidate the request before the vault is touched.
            status = fixture.request(
                name='identity.custodial_status',
                actor='cust',
                token=True,
                token_id=custody['credential_id'],
                request_id='native-vault-sign',
                arguments={},
            )
            status = replace(status, arguments={})
            status = replace(status, payload_digest=digest(payload_fields(status)))
            signed = native.call('vault_sign', request=wire(status))
            stored = fixture.conn.execute(
                'SELECT body FROM credentials WHERE id=?', (custody['identity_key_id'],)
            ).fetchone()[0]
            verify(
                unb64(json.loads(stored)['verifier']),
                b'fixture revision',
                decode(Signature, signed['signature']),
                purpose='revision',
            )
            fixture.conn.execute(
                "UPDATE custodial_vault SET status='decrypt_only' WHERE subject=?",
                (custody['subject_id'],),
            )
            native.call('vault_sign', request=wire(status), error='custodial_vault_unavailable')

            fixture.setting('recovery_quarantine', False)
            native.call('pending', error='recovery_quarantined')
            fixture.conn.execute("DELETE FROM settings WHERE key='recovery_quarantine'")
            native.call('pending')
            fixture.setting('recovery_runtime_generation', 'restored')
            native.call('pending', error='recovery_runtime_stale')
            fixture.conn.execute("DELETE FROM settings WHERE key='recovery_runtime_generation'")
            native.call('pending', error='recovery_runtime_stale')
            return {
                'ok': True,
                'native_transactions': native.checks,
                'backend': 'postgresql',
                'checks': [
                    'registration_ca_interop',
                    'csr_publication',
                    'narrow_ceiling_denied',
                    'metadata_replay_scope',
                    'atomic_rollback',
                    'result_replay',
                    'single_delivery',
                    'token_source_survives_rotation',
                    'key_approved_browser',
                    'custodial_vault_signing',
                    'recovery_fence',
                ],
            }
        finally:
            native.close()
            await fixture.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    report = asyncio.run(exercise(args.binary.resolve()))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
