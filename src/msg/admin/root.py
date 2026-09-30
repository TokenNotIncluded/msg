"""Local root administration; never imported by a network adapter or worker.

The underscored provisioning functions are internal local use cases. The command
entrypoint must pass OS-console checks before calling them. Tests call these use
cases only against disposable directories and never enable a network root route.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import re
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from msg.bootstrap import bootstrap, seed_resource
from msg.constants import (
    ADMINS_GROUP as ADMINS_GROUP,
    CA_SPACE as CA_SPACE,
    CERT_SPACE as CERT_SPACE,
    CSR_SPACE as CSR_SPACE,
    ONLINE_CA as ONLINE_CA,
    PROTOCOL_VERSION as PROTOCOL_VERSION,
    PUBLIC_GROUP as PUBLIC_GROUP,
    ROOT_SPACE as ROOT_SPACE,
    ROOT_SUBJECT as ROOT_SUBJECT,
    TOOLS_SPACE as TOOLS_SPACE,
)
from msg.core.codec import b64, canonical, digest, loads, unb64, wire
from msg.core.errors import require
from msg.core.models import (
    AuditEvent,
    Certificate,
    CertificateRequest,
    Credential,
    Event,
    IssuancePolicy,
    ResourceRef,
    Signature,
)
from msg.plugins.common import new_id
from msg.plugins.identity import certificate_resource
from msg.security.capabilities import grant_for
from msg.security.certificates import csr_body, sign_certificate, verify_csr
from msg.security.crypto import Ed25519Signer, open_private_key, seal_private_key
from msg.security.trust_files import reserved_plugins_directory, trust_file, write_trust
from msg.storage.git import durable_write


def root_envelope(config_dir):
    from msg.config import root_private_dir

    current = root_private_dir(Path(config_dir)) / 'key.json'
    legacy = Path(config_dir) / 'root' / 'key.json'
    return legacy if legacy.is_file() and not current.exists() else current


async def _provision(app, pin):
    """Initialize only an empty installation; partial state requires explicit recovery."""
    settings = app.settings
    protected = settings.root_private_dir
    marker = settings.config_dir / 'initialization.pending'
    require(
        not root_envelope(settings.config_dir).exists()
        and not settings.trust_file.exists()
        and not marker.exists(),
        'initialization_requires_recovery',
    )
    await app.open_storage()
    async with app.metadata.transaction(write=False) as tx:
        require(
            tx.one('SELECT COUNT(*) FROM resources')[0] == 0, 'initialization_requires_recovery'
        )
    root = Ed25519Signer.generate()
    envelope = seal_private_key(root.private_bytes(), pin)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    reserved_plugins_directory(settings.config_dir, create=True)
    durable_write(marker, b'1\n', mode=0o600)
    protected.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(protected, 0o700)
    settings.service_keys.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(settings.service_keys, 0o700)
    settings.trust_file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    # Wrapped before touching the installation; a rejected PIN leaves no partial state.
    durable_write(root_envelope(settings.config_dir), canonical(envelope), mode=0o600)
    online = Ed25519Signer.generate()
    receipt = Ed25519Signer.generate()
    durable_write(settings.service_keys / 'online.key', online.private_bytes(), mode=0o600)
    durable_write(settings.service_keys / 'receipt.key', receipt.private_bytes(), mode=0o600)
    durable_write(settings.service_keys / 'tokens.key', os.urandom(32), mode=0o600)
    now = app.clock()
    await bootstrap(
        app.metadata, app.contents, app.registry, now, selftest_run_id=app.selftest_run_id
    )
    root_grants = app.primary_ceiling()
    root_certificate = Certificate(
        resource_id='cert_root',
        serial=new_id('serial'),
        subject_id=ROOT_SUBJECT,
        key_id=root.key_id,
        issuer_id=ROOT_SUBJECT,
        parent_certificate_id=None,
        authority_sources=(),
        kind='ca',
        grants=root_grants,
        not_before=now,
        expires_at=now + timedelta(days=3650),
        target_service=settings.service_url,
        delegation_depth=8,
        issuance=IssuancePolicy(
            issue_grants=root_grants,
            max_cert_ttl_seconds=31536000,
            max_child_ca_depth=3,
            max_delegation_depth=8,
        ),
        signature=Signature(key_id=root.key_id, algorithm='ed25519', value=b''),
    )
    root_certificate = sign_certificate(root_certificate, root)
    online_use = grant_for(app.registry.capability('cert.issue'), scope=app.default_scope())
    csr = CertificateRequest(
        resource_id=new_id('csr'),
        applicant=ONLINE_CA,
        subject_id=ONLINE_CA,
        requested_issuer=ROOT_SUBJECT,
        public_key=online.public_key,
        kind='ca',
        grants=(online_use,),
        issuance=IssuancePolicy(
            issue_grants=app.base_grants(),
            max_cert_ttl_seconds=settings.base_certificate_ttl,
            max_child_ca_depth=0,
            max_delegation_depth=8,
        ),
        requested_ttl_seconds=31536000,
        target_service=settings.service_url,
        delegation_depth=0,
        authority_sources=(),
        request_digest='',
        possession_proof=Signature(key_id=online.key_id, algorithm='ed25519', value=b''),
    )
    csr = replace(
        csr,
        request_digest=digest(csr_body(csr)),
        possession_proof=online.sign(canonical(csr_body(csr)), purpose='csr'),
    )
    async with app.metadata.transaction(write=True) as tx:
        for uid, signer, ceiling in ((ROOT_SUBJECT, root, ()), (ONLINE_CA, online, (online_use,))):
            await tx.save_credential(
                Credential(
                    id=signer.key_id,
                    subject_id=uid,
                    kind='signing_key',
                    verifier=signer.public_key,
                    ceiling=ceiling,
                    not_before=now,
                    expires_at=None,
                    revoked_at=None,
                ),
                0,
            )
            for name in ('keys', 'certificates'):
                await seed_resource(
                    tx,
                    app.contents,
                    {
                        'id': 'r_' + digest((uid, name))[7:39],
                        'type': 'topic',
                        'name': name,
                        'parent': uid,
                        'owner': uid,
                        'group': PUBLIC_GROUP,
                        'mode': '0555',
                    },
                    now,
                )
        await certificate_resource(tx, root_certificate, now)
        await tx.register_certificate(root_certificate, None, 0)
        await seed_resource(
            tx,
            app.contents,
            {
                'id': csr.resource_id,
                'type': 'csr',
                'name': csr.resource_id,
                'parent': CSR_SPACE,
                'owner': ONLINE_CA,
                'group': ADMINS_GROUP,
                'mode': '0600',
            },
            now,
        )
        await tx.save_csr(csr)
        tx.set_setting('active_root_certificate', root_certificate.resource_id)
        tx.set_setting('online_ca_request', csr.resource_id)
        tx.set_setting(
            'receipt_public_key', {'key_id': receipt.key_id, 'public_key': b64(receipt.public_key)}
        )
    write_trust(
        settings.config_dir,
        {'version': 1, 'public_key': b64(root.public_key), 'certificate': wire(root_certificate)},
        writer=durable_write,
    )
    marker.unlink()
    await app.load()
    return csr.resource_id, root


async def _approve_csr(app, csr_id, signer, *, expected_digest, operator):
    now = app.clock()
    async with app.metadata.transaction(write=True) as tx:
        csr = await tx.csr(csr_id)
        state = await tx.csr_state(csr_id)
        require(csr.requested_issuer == ROOT_SUBJECT, 'not_root_request')
        if expected_digest is not None:
            require(csr.request_digest == expected_digest, 'csr_changed_after_confirmation')
        verify_csr(csr)
        if state.status == 'issued':
            return await tx.certificate(state.certificate_id)
        require(state.status == 'pending', 'csr_not_pending')
        require(signer.key_id == app.certificates.root_certificate.key_id, 'root_key_mismatch')
        parent = app.certificates.root_certificate
        certificate = Certificate(
            resource_id=new_id('cert'),
            serial=new_id('serial'),
            subject_id=csr.subject_id,
            key_id=csr.possession_proof.key_id,
            issuer_id=ROOT_SUBJECT,
            parent_certificate_id=parent.resource_id,
            authority_sources=csr.authority_sources,
            kind=csr.kind,
            grants=csr.grants,
            not_before=now,
            expires_at=min(now + timedelta(seconds=csr.requested_ttl_seconds), parent.expires_at),
            target_service=csr.target_service,
            delegation_depth=csr.delegation_depth,
            issuance=csr.issuance,
            signature=Signature(key_id=signer.key_id, algorithm='ed25519', value=b''),
        )
        certificate = sign_certificate(certificate, signer)
        await certificate_resource(tx, certificate, now)
        await app.certificates.validate_publication(certificate, csr, tx)
        await tx.register_certificate(certificate, csr_id, state.generation)
        credential = await tx.credential(certificate.key_id)
        if not credential.ceiling:
            subject = await tx.subject(credential.subject_id)
            await tx.save_credential(
                replace(credential, ceiling=certificate.grants), subject.auth_version
            )
        if csr_id == tx.setting('online_ca_request'):
            tx.set_setting('online_ca_certificate', certificate.resource_id)
        event = Event(
            id=new_id('audit'),
            type='root.cert.issue',
            time=now,
            request_id=csr_id,
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            resources=(ResourceRef(id=certificate.resource_id),),
            data={
                'operator': operator,
                'csr_digest': csr.request_digest,
                'grants': wire(certificate.grants),
            },
        )
        await tx.append_audit(
            AuditEvent(
                event=event,
                authority=(ResourceRef(id=parent.resource_id),),
                before_digest=csr.request_digest,
                after_digest=digest(certificate),
                previous_digest=None,
                entry_digest='',
                result='issued',
            )
        )
        return certificate


def require_local_console(config_dir):
    """OS root plus a local VT/serial console, not an arbitrary pseudo-terminal.

    Remote SSH and terminal multiplexers normally use /dev/pts and cannot satisfy
    this boundary merely by deleting SSH_* variables or supplying client=cli.
    Deployments that need another administrative channel must use a separately
    audited OS policy, not relax this check through the network configuration.
    """
    require(hasattr(os, 'geteuid') and os.geteuid() == 0, 'local_os_administrator_required')
    require(
        not any(name in os.environ for name in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY')),
        'remote_admin_forbidden',
    )
    require(sys.stdin.isatty() and sys.stdout.isatty(), 'local_console_required')
    tty = os.path.realpath(os.ttyname(sys.stdin.fileno()))
    require(
        re.fullmatch(r'/dev/(console|tty[0-9]+|ttyS[0-9]+|hvc[0-9]+)', tty) is not None,
        'local_console_required',
    )
    path = Path(config_dir).resolve()
    require(
        path.exists() and path.stat().st_uid == 0 and not path.stat().st_mode & 0o022,
        'unsafe_config_owner',
    )
    return f'uid:{os.geteuid()}:{tty}'


def require_ssh_administrator(config_dir):
    """Explicit SSH opt-in for initialization and issuance, never HTTP administration."""
    require(hasattr(os, 'geteuid') and os.geteuid() == 0, 'local_os_administrator_required')
    require(bool(os.environ.get('SSH_CONNECTION')), 'ssh_administrator_required')
    require(sys.stdin.isatty() and sys.stdout.isatty(), 'interactive_pin_required')
    tty = os.path.realpath(os.ttyname(sys.stdin.fileno()))
    require(re.fullmatch(r'/dev/pts/[0-9]+', tty) is not None, 'ssh_terminal_required')
    path = Path(config_dir).resolve()
    require(
        path.exists() and path.stat().st_uid == 0 and not path.stat().st_mode & 0o022,
        'unsafe_config_owner',
    )
    return f'uid:{os.geteuid()}:ssh:{tty}:{os.environ["SSH_CONNECTION"]}'


class RootAdmin:
    def __init__(self, config_dir=Path('/etc/msgd'), *, allow_ssh=False):
        self.config_dir = Path(config_dir)
        self.allow_ssh = allow_ssh

    def _provisioning_operator(self):
        check = require_ssh_administrator if self.allow_ssh else require_local_console
        return check(self.config_dir)

    def _app(self):
        from msg.application import Application
        from msg.config import load_settings

        return Application(load_settings(self.config_dir))

    def initialize(self):
        self._provisioning_operator()
        app = self._app()
        if root_envelope(self.config_dir).exists() and app.settings.trust_file.exists():
            status = self.doctor()
            require(
                all(
                    status['checks'].get(k, {}).get('ok')
                    for k in ('storage', 'root_trust', 'bootstrap', 'root_private_boundary')
                ),
                'initialization_requires_recovery',
            )
            private = open_private_key(
                loads(root_envelope(self.config_dir).read_bytes()),
                getpass.getpass('Current root PIN/passphrase: '),
            )
            trust = loads(app.settings.trust_file.read_bytes())
            require(
                Ed25519Signer.from_bytes(private).public_key == unb64(trust['public_key']),
                'root_key_mismatch',
            )
            return status['root_id']
        first = getpass.getpass('New root PIN/passphrase: ')
        second = getpass.getpass('Confirm root PIN/passphrase: ')
        require(first == second, 'pin_confirmation_mismatch')
        csr, _ = asyncio.run(_provision(app, first))
        print(
            canonical({
                'status': 'initialized',
                'root_id': ROOT_SUBJECT,
                'online_ca_request': csr,
            }).decode()
        )
        return ROOT_SUBJECT

    def issue(self, csr_id):
        operator = self._provisioning_operator()
        app = self._app()

        async def read():
            await app.load()
            async with app.metadata.transaction(write=False) as tx:
                return await tx.csr(csr_id), await tx.csr_state(csr_id)

        csr, state = asyncio.run(read())
        # json.dumps escapes terminal controls; no raw user-controlled text is printed.
        print(
            canonical({
                'request': wire(csr),
                'state': wire(state),
                'public_key_fingerprint': digest(csr.public_key),
            }).decode()
        )
        if state.status == 'issued':

            async def previous():
                async with app.metadata.transaction(write=False) as tx:
                    return await tx.certificate(state.certificate_id)

            return asyncio.run(previous())
        require(
            input('Type the exact CSR digest to approve: ').strip() == csr.request_digest,
            'approval_cancelled',
        )
        pin = getpass.getpass('Root PIN/passphrase: ')
        private = open_private_key(loads(root_envelope(self.config_dir).read_bytes()), pin)
        signer = Ed25519Signer.from_bytes(private)
        return asyncio.run(
            _approve_csr(app, csr_id, signer, expected_digest=csr.request_digest, operator=operator)
        )

    def change_pin(self):
        require_local_console(self.config_dir)
        require(input('Type CHANGE PIN to continue: ') == 'CHANGE PIN', 'approval_cancelled')
        path = root_envelope(self.config_dir)
        private = open_private_key(
            loads(path.read_bytes()), getpass.getpass('Current root PIN/passphrase: ')
        )
        first = getpass.getpass('New root PIN/passphrase: ')
        require(
            first == getpass.getpass('Confirm new root PIN/passphrase: '),
            'pin_confirmation_mismatch',
        )
        durable_write(path, canonical(seal_private_key(private, first)), mode=0o600)

    def rotate(self, *, lost_key=False, resume=False):
        operator = require_local_console(self.config_dir)
        app = self._app()
        from msg.admin.rotation import complete, journal_path, prepare

        if resume:
            require(journal_path(app).is_file(), 'rotation_journal_missing')
            journal = loads(journal_path(app).read_bytes())
            print(canonical(journal['statement']).decode())
            require(
                input('Type the new root fingerprint to resume: ')
                == journal['statement']['new_fingerprint'],
                'approval_cancelled',
            )
            pin = getpass.getpass('New root PIN/passphrase: ')
        else:
            asyncio.run(app.load())
            print(
                canonical({
                    'old_fingerprint': digest(app.certificates.root_public_key),
                    'effect': 'all existing certificate chains require reissuance',
                    'lost_key': lost_key,
                }).decode()
            )
            require(
                input('Type ROTATE ROOT ' + digest(app.certificates.root_public_key) + ': ')
                == 'ROTATE ROOT ' + digest(app.certificates.root_public_key),
                'approval_cancelled',
            )
            old = (
                None
                if lost_key
                else Ed25519Signer.from_bytes(
                    open_private_key(
                        loads(root_envelope(self.config_dir).read_bytes()),
                        getpass.getpass('Current root PIN/passphrase: '),
                    )
                )
            )
            pin = getpass.getpass('New root PIN/passphrase: ')
            require(
                pin == getpass.getpass('Confirm new root PIN/passphrase: '),
                'pin_confirmation_mismatch',
            )
            journal = prepare(app, Ed25519Signer.generate(), pin, old_signer=old, operator=operator)
        return asyncio.run(complete(app, journal, pin=pin))

    def doctor(self):
        # Imported lazily: this read-only diagnostic must not run schema migrations.
        from msg.admin.diagnostics import doctor

        return doctor(self.config_dir)

    def selftest(self):
        from msg.admin.diagnostics import selftest

        return asyncio.run(selftest())

    def revoke(self, certificate_id, reason):
        operator = require_local_console(self.config_dir)
        app = self._app()
        asyncio.run(app.load())

        async def read():
            async with app.metadata.transaction(write=False) as tx:
                return await tx.certificate(certificate_id)

        certificate = asyncio.run(read())
        print(canonical({'certificate': wire(certificate), 'reason': reason}).decode())
        require(
            input('Type REVOKE ' + certificate_id + ' to continue: ') == 'REVOKE ' + certificate_id,
            'approval_cancelled',
        )
        private = open_private_key(
            loads(root_envelope(self.config_dir).read_bytes()),
            getpass.getpass('Root PIN/passphrase: '),
        )
        return asyncio.run(
            _revoke(
                app,
                certificate_id,
                Ed25519Signer.from_bytes(private),
                reason=reason,
                operator=operator,
            )
        )

    def sign_recovery_proof(self, source_backup_sha256, sequence, destination):
        require_local_console(self.config_dir)
        from msg.admin.backup_retirement import write_record
        from msg.admin.recovery_proof import draft, open_for_proof, seal
        from msg.security.root_files import read_private

        app = asyncio.run(open_for_proof(self.config_dir))
        try:
            body = asyncio.run(
                draft(
                    app,
                    public_key=app.certificates.root_public_key,
                    source_backup_sha256=source_backup_sha256,
                    sequence=sequence,
                )
            )
            fingerprint = digest(body)
            print(
                canonical({
                    'service': body['service'],
                    'sequence': sequence,
                    'source_backup_sha256': source_backup_sha256,
                    'state_digest': fingerprint,
                    'tables': len(body['metadata']['tables']),
                    'expires_at': body['expires_at'],
                }).decode()
            )
            require(
                input('Type SIGN COMPLETE RECOVERY ' + fingerprint + ': ')
                == 'SIGN COMPLETE RECOVERY ' + fingerprint,
                'approval_cancelled',
            )
            envelope = loads(read_private(root_envelope(self.config_dir)))
            signer = Ed25519Signer.from_bytes(
                open_private_key(envelope, getpass.getpass('Root PIN/passphrase: '))
            )
            packet = asyncio.run(seal(app, body, signer))
            write_record(Path(destination), packet)
            return {
                'status': 'complete_recovery_state_signed',
                'digest': fingerprint,
                'independent_pin_required': True,
                'path': str(destination),
            }
        finally:
            asyncio.run(app.close())

    def promote_recovery(self, source, independent_trust):
        operator = require_local_console(self.config_dir)
        from msg.admin.recovery_proof import (
            IndependentRecoveryPin,
            open_for_proof,
            promote,
            verify_proof,
        )
        from msg.security.root_files import read_private

        packet = loads(read_private(source, limit=64 * 1024 * 1024))
        trust = loads(read_private(independent_trust, limit=65536))
        require(
            type(trust) is dict
            and set(trust)
            == {'service', 'public_key', 'digest', 'sequence', 'source_backup_sha256'},
            'recovery_independent_pin_required',
        )
        pin = IndependentRecoveryPin(**dict(trust, public_key=unb64(trust['public_key'], limit=32)))
        app = asyncio.run(open_for_proof(self.config_dir))
        try:
            body = verify_proof(packet, pin, app.clock())
            print(
                canonical({
                    'service': pin.service,
                    'state_digest': pin.digest,
                    'sequence': pin.sequence,
                    'source_backup_sha256': pin.source_backup_sha256,
                    'tables': len(body['metadata']['tables']),
                    'effect': 'release verified complete recovery; restart all runtimes',
                }).decode()
            )
            require(
                input('Type PROMOTE COMPLETE RECOVERY ' + pin.digest + ': ')
                == 'PROMOTE COMPLETE RECOVERY ' + pin.digest,
                'approval_cancelled',
            )
            envelope = loads(read_private(root_envelope(self.config_dir)))
            signer = Ed25519Signer.from_bytes(
                open_private_key(envelope, getpass.getpass('Root PIN/passphrase: '))
            )
            return asyncio.run(promote(app, packet, pin=pin, signer=signer, operator=operator))
        finally:
            asyncio.run(app.close())

    def sign_backup_retirement(self, source, destination):
        """Root-sign an operator's statement that listed backup sets lost one old key."""
        require_local_console(self.config_dir)
        from msg.admin.backup_retirement import read_statement, unsigned_statement, write_record
        from msg.security.backup_retirement import sign_record

        target = Path(destination)
        require(
            not target.exists() and not target.is_symlink(), 'backup_retirement_destination_exists'
        )
        body = unsigned_statement(read_statement(source))
        print(
            canonical({
                'statement': body,
                'digest': digest(body),
                'claim': 'listed_backup_sets_only',
            }).decode()
        )
        require(
            input('Type ATTEST BACKUP RETIREMENT ' + digest(body) + ': ')
            == 'ATTEST BACKUP RETIREMENT ' + digest(body),
            'approval_cancelled',
        )
        from msg.security.root_files import read_private

        envelope = loads(read_private(root_envelope(self.config_dir)))
        private = open_private_key(envelope, getpass.getpass('Root PIN/passphrase: '))
        record = sign_record(body, Ed25519Signer.from_bytes(private))
        write_record(target, record)
        return {'status': 'backup_retirement_signed', 'path': str(target), 'digest': digest(record)}

    def import_backup_retirement(self, source):
        """Only this console path persists a record; reads re-verify it every time."""
        operator = require_local_console(self.config_dir)
        from msg.admin.backup_retirement import import_record, read_statement

        app = self._app()
        record = read_statement(source)
        print(canonical({'record': record, 'digest': digest(record)}).decode())
        require(
            input('Type IMPORT BACKUP RETIREMENT ' + digest(record) + ': ')
            == 'IMPORT BACKUP RETIREMENT ' + digest(record),
            'approval_cancelled',
        )

        async def run():
            await app.load()
            try:
                async with app.metadata.transaction(write=True) as tx:
                    return await import_record(tx, record, now=app.clock(), operator=operator)
            finally:
                await app.close()

        attestation = asyncio.run(run())
        return {
            'status': 'backup_retirement_imported',
            'scope': 'listed_backup_sets_only',
            **attestation.view(),
        }

    def backup(self, destination):
        require_local_console(self.config_dir)
        target = Path(destination)
        require(not target.exists() and not target.is_symlink(), 'backup_destination_exists')
        envelope = loads(root_envelope(self.config_dir).read_bytes())
        trust = loads(trust_file(self.config_dir).read_bytes())
        require(input('Type BACKUP ROOT to continue: ') == 'BACKUP ROOT', 'approval_cancelled')
        private = open_private_key(envelope, getpass.getpass('Root PIN/passphrase: '))
        require(
            b64(Ed25519Signer.from_bytes(private).public_key) == trust['public_key'],
            'root_key_mismatch',
        )
        durable_write(
            target,
            canonical({'format': 'msg-root-backup-v1', 'envelope': envelope, 'trust': trust}),
            mode=0o600,
        )
        return {
            'status': 'encrypted_backup_created',
            'path': str(target),
            'fingerprint': digest(unb64(trust['public_key'])),
        }

    def recover(self, source):
        require_local_console(self.config_dir)
        target = root_envelope(self.config_dir)
        require(not target.exists(), 'root_material_already_present')
        backup = loads(Path(source).read_bytes())
        require(
            set(backup) == {'format', 'envelope', 'trust'}
            and backup['format'] == 'msg-root-backup-v1',
            'invalid_root_backup',
        )
        current = loads(trust_file(self.config_dir).read_bytes())
        require(canonical(current) == canonical(backup['trust']), 'trust_anchor_mismatch')
        print(
            canonical({
                'fingerprint': digest(unb64(current['public_key'])),
                'action': 'recover_missing_encrypted_root_key',
            }).decode()
        )
        require(input('Type RECOVER ROOT to continue: ') == 'RECOVER ROOT', 'approval_cancelled')
        private = open_private_key(
            backup['envelope'], getpass.getpass('Backup root PIN/passphrase: ')
        )
        require(
            b64(Ed25519Signer.from_bytes(private).public_key) == current['public_key'],
            'root_key_mismatch',
        )
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        durable_write(target, canonical(backup['envelope']), mode=0o600)
        return {'status': 'root_key_recovered', 'fingerprint': digest(unb64(current['public_key']))}


async def _revoke(app, certificate_id, signer, *, reason, operator):
    require(
        certificate_id != app.certificates.root_certificate.resource_id, 'root_rotation_required'
    )
    require(signer.key_id == app.certificates.root_certificate.key_id, 'root_key_mismatch')
    async with app.metadata.transaction(write=True) as tx:
        certificate = await tx.certificate(certificate_id)
        statement = {
            'certificate_id': certificate_id,
            'reason': reason,
            'time': wire(app.clock()),
            'issuer': ROOT_SUBJECT,
            'operator': operator,
        }
        signature = signer.sign(canonical(statement), purpose='revocation')
        event = Event(
            id=new_id('audit'),
            type='root.cert.revoke',
            time=app.clock(),
            request_id=new_id('local'),
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            resources=(ResourceRef(id=certificate_id),),
            data={**statement, 'signature': wire(signature)},
        )
        await tx.revoke_certificate(
            certificate_id,
            AuditEvent(
                event=event,
                authority=(ResourceRef(id=app.certificates.root_certificate.resource_id),),
                before_digest=digest(certificate),
                after_digest=digest(statement),
                previous_digest=None,
                entry_digest='',
                result='revoked',
            ),
        )
    return {'certificate_id': certificate_id, 'revoked': True, 'signature': wire(signature)}
