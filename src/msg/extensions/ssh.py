"""OpenSSH forced-command adapter. No PTY, forwarding, shell or root proxy.

Only this OS-authenticated adapter constructs an SSH principal. A source field,
IP address, token, or network request can never opt into SSH authentication.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, decode, digest
from msg.core.errors import Failure, require
from msg.core.models import (
    CapabilityGrant,
    Credential,
    EffectJob,
    HandlerOutput,
    Principal,
    ResourceRef,
    Signature,
    SignatureProof,
)
from msg.core.packet import result_wire
from msg.core.requests import payload_fields, request_for
from msg.plugins.common import check_access, new_id, resolve
from msg.plugins.communication import event_id
from msg.plugins.identity import controlled_owner, validate_ceiling
from msg.plugins.schemas import GRANTS, IDENTIFIER, SIGNATURE, STRING, obj
from msg.security.crypto import key_id, verify


def public_key(line):
    require(isinstance(line, str) and '\n' not in line and '\r' not in line, 'invalid_ssh_key')
    fields = line.split()
    require(len(fields) in {2, 3} and fields[0] == 'ssh-ed25519', 'unsupported_ssh_key')
    normalized = ' '.join(fields[:2])
    try:
        key = serialization.load_ssh_public_key(normalized.encode('ascii'))
        require(isinstance(key, Ed25519PublicKey), 'unsupported_ssh_key')
        return normalized, key.public_bytes_raw()
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Failure('invalid_ssh_key') from exc


def parse_command(command):
    require(
        isinstance(command, str)
        and 0 < len(command) <= 32768
        and not any(ord(c) < 32 or ord(c) == 127 for c in command),
        'ssh_command_denied',
    )
    try:
        args = shlex.split(command, posix=True)
    except ValueError as exc:
        raise Failure('ssh_command_denied') from exc
    if len(args) == 2 and args[0] in {'git-upload-pack', 'git-receive-pack'}:
        require(
            re.fullmatch(r'/[@&][a-z][a-z0-9-]{1,40}/[A-Za-z0-9][A-Za-z0-9_.-]{0,90}\.git', args[1])
            is not None,
            'ssh_command_denied',
        )
        return args[0], args[1]
    if len(args) == 4 and args[:2] == ['msg', 'call']:
        require(re.fullmatch(r'[a-z][a-z0-9_.]+', args[2]) is not None, 'ssh_command_denied')
        return 'call', args[2], args[3]
    if len(args) == 3 and args[:2] == ['msg', 'packet']:
        require(re.fullmatch(r'[A-Za-z0-9_-]+', args[2]) is not None, 'ssh_command_denied')
        return 'packet', args[2]
    raise Failure('ssh_command_denied')


def require_sshd_process():
    """Check an actual root-owned sshd ancestor, not a client-supplied environment."""
    require(sys.platform == 'linux' and os.geteuid() != 0, 'ssh_os_isolation_required')
    pid = os.getppid()
    for _ in range(6):
        if pid <= 1:
            break
        directory = Path('/proc') / str(pid)
        try:
            name = (directory / 'comm').read_text().strip()
            status = dict(
                line.split(':', 1)
                for line in (directory / 'status').read_text().splitlines()
                if ':' in line
            )
            if name in {'sshd', 'sshd-session', 'sshd-auth'}:
                # Linux may hide /proc/<root-pid>/exe from an unprivileged
                # child. The root-owned process identity cannot be forged by
                # the dedicated service account; arbitrary ancestor names can.
                uids = [int(value) for value in status['Uid'].split()]
                if (
                    directory.stat().st_uid == 0
                    and len(uids) == 4
                    and all(uid == 0 for uid in uids)
                ):
                    return
                # A root-owned executable proves nothing: an unprivileged
                # Python/shell process can set its comm to "sshd". Continue to
                # the privileged monitor/listener instead of trusting that
                # process, including a user-launched OpenSSH daemon.
            pid = int(status['PPid'].strip())
        except OSError, ValueError, KeyError:
            break
    raise Failure('ssh_os_isolation_required')


async def ssh_principal(app, credential_id, tx):
    require(
        tx.setting('active_root_certificate', app.certificates.root_certificate.resource_id)
        == app.certificates.root_certificate.resource_id,
        'service_restart_required',
    )
    credential = await tx.credential(credential_id)
    require(credential.kind == 'ssh_key', 'wrong_credential_kind')
    require(not tx.setting('identity_archived:' + credential.subject_id), 'account_archived')
    subject = await tx.subject(credential.subject_id)
    require(subject.resource_id != ROOT_SUBJECT and not subject.local_only, 'local_only')
    now = app.clock()
    require(credential.revoked_at is None, 'credential_revoked')
    require(
        credential.not_before <= now
        and (credential.expires_at is None or now < credential.expires_at),
        'credential_expired',
    )
    require(credential.ceiling, 'credential_ceiling')
    certificates = tuple(tx.setting('ssh_certificates:' + credential_id, []))
    principal = Principal(
        actor=subject.resource_id,
        subject=subject.resource_id,
        credential_id=credential.id,
        method='ssh',
        certificates=certificates,
        ceiling=credential.ceiling,
    )
    await app.authorizer.grants(principal, tx)
    return principal


class SSHAuthenticator:
    """Bound to the credential OpenSSH has already authenticated, never wire data."""

    def __init__(self, app, credential_id):
        self.app, self.credential_id = app, credential_id

    async def authenticate(self, request, session, *, entry):
        require(entry == 'network', 'entry_not_allowed')
        principal = await ssh_principal(self.app, self.credential_id, session)
        spec = self.app.registry.operation(request.operation, request.contract_version)
        require('network' in spec.entries, 'entry_not_allowed')
        require(
            request.protocol_version == 1
            and request.target_service == self.app.settings.service_url,
            'wrong_service',
        )
        require(
            request.payload_digest == digest(payload_fields(request)), 'payload_digest_mismatch'
        )
        require(
            request.expires_at is not None
            and self.app.clock() < request.expires_at
            and (request.expires_at - self.app.clock()).total_seconds() <= 300,
            'request_expired',
        )
        require(request.subject in {None, principal.subject}, 'ssh_subject_mismatch')
        require(
            any(f'{spec.name}@{spec.version}' in g.operations for g in principal.ceiling),
            'credential_ceiling',
        )
        if request.proof is not None:
            require(isinstance(request.proof, SignatureProof), 'ssh_signed_packet_required')
            require(
                request.proof.signature.key_id == self.credential_id, 'ssh_signature_key_mismatch'
            )
            credential = await session.credential(self.credential_id)
            from msg.core.requests import signing_bytes

            verify(
                credential.verifier,
                signing_bytes(request),
                request.proof.signature,
                purpose='request',
            )
            # Bind the portable signature to the same SSH key. This preserves
            # channel revocation for delayed jobs, rather than losing it by
            # substituting another application key after the handshake.
            for id in request.proof.certificates:
                cert = await self.app.certificates.validate(id, session)
                require(
                    cert.key_id == credential.id and cert.subject_id == principal.subject,
                    'certificate_subject_mismatch',
                )
            return replace(principal, method='signature', certificates=request.proof.certificates)
        require(not spec.require_signature, 'signature_required')
        # A successful SSH handshake is not a portable digitally signed ACK.
        require(spec.name != 'discussion.ack', 'ack_signature_or_token_required')
        return principal


def register(app, op):
    @op(
        'identity.ssh_key_add',
        obj(
            {'public_key': STRING, 'proof': SIGNATURE, 'ceiling': GRANTS},
            ('public_key', 'proof', 'ceiling'),
        ),
        signature=True,
    )
    async def add(ctx, request, tx):
        subject = await controlled_owner(app, ctx, request, tx)
        require(subject.kind == 'registered', 'registered_identity_required')
        line, public = public_key(request.arguments['public_key'])
        verify(
            public,
            canonical({'subject_id': subject.resource_id, 'public_key': line}),
            decode(Signature, request.arguments['proof']),
            purpose='ssh-key-add',
        )
        grants = tuple(decode(CapabilityGrant, g) for g in request.arguments['ceiling'])
        require(grants, 'credential_ceiling')
        await validate_ceiling(app, ctx, tx, grants)
        id = key_id(public)
        require(
            tx.one('SELECT id FROM credentials WHERE id=?', (id,)) is None,
            'credential_already_registered',
        )
        await tx.save_credential(
            Credential(
                id=id,
                subject_id=subject.resource_id,
                kind='ssh_key',
                verifier=public,
                ceiling=grants,
                not_before=ctx.now,
                expires_at=None,
                revoked_at=None,
            ),
            subject.auth_version,
        )
        tx.set_setting('ssh_public:' + id, line)
        return HandlerOutput(data={'key_id': id, 'subject_id': subject.resource_id})

    @op('identity.ssh_key_revoke', obj({'key_id': IDENTIFIER}, ('key_id',)), signature=True)
    async def revoke(ctx, request, tx):
        subject = await controlled_owner(app, ctx, request, tx)
        key = await tx.credential(request.arguments['key_id'])
        require(
            key.kind == 'ssh_key' and key.subject_id == subject.resource_id,
            'credential_subject_mismatch',
        )
        await tx.save_credential(replace(key, revoked_at=ctx.now), subject.auth_version)
        return HandlerOutput(data={'key_id': key.id, 'revoked': True})

    @op(
        'identity.ssh_certificates',
        obj(
            {
                'key_id': IDENTIFIER,
                'certificates': {'type': 'array', 'items': IDENTIFIER, 'uniqueItems': True},
            },
            ('key_id', 'certificates'),
        ),
        signature=True,
    )
    async def certificates(ctx, request, tx):
        subject = await controlled_owner(app, ctx, request, tx)
        key = await tx.credential(request.arguments['key_id'])
        require(
            key.kind == 'ssh_key' and key.subject_id == subject.resource_id,
            'credential_subject_mismatch',
        )
        for id in request.arguments['certificates']:
            cert = await app.certificates.validate(id, tx)
            require(
                cert.key_id == key.id and cert.subject_id == subject.resource_id,
                'certificate_subject_mismatch',
            )
        tx.set_setting('ssh_certificates:' + key.id, request.arguments['certificates'])
        return HandlerOutput(
            data={'key_id': key.id, 'certificates': request.arguments['certificates']}
        )

    @op('git.receive', obj({'id': IDENTIFIER}, ('id',)), effect='external')
    async def receive(ctx, request, tx):
        require(ctx.principal.method == 'ssh', 'ssh_transport_required')
        rid = await resolve(tx, request.arguments['id'])
        require((await tx.resource(rid)).type == 'repo', 'not_a_repository')
        await check_access(app, ctx, request, tx, rid, 'write')
        id, eid = new_id('job'), event_id(request, ctx.principal.subject)
        job = EffectJob(
            id=id,
            event_id=eid,
            kind='git.receive',
            dedupe_key='git-receive:' + eid,
            principal=ctx.principal,
            operation='git.receive',
            arguments={
                'contract_version': request.contract_version,
                'id': rid,
                'request_id': request.request_id,
            },
            state='running',
            attempts=1,
            next_attempt_at=ctx.now,
            lease_until=ctx.now + timedelta(seconds=600),
        )
        await tx.enqueue(job)
        return HandlerOutput(resources=(ResourceRef(id=rid),), data={'job_id': id})


async def authorized_key(config_dir, key_type, key_body):
    from msg.application import Application
    from msg.config import load_settings

    line, public = public_key(key_type + ' ' + key_body)
    app = Application(load_settings(config_dir))
    await app.load()
    try:
        async with app.metadata.transaction(write=False) as tx:
            principal = await ssh_principal(app, key_id(public), tx)
            require(tx.setting('ssh_public:' + principal.credential_id) == line, 'invalid_ssh_key')
        command = shlex.join([
            sys.executable,
            '-m',
            'msg.daemon',
            '--config-dir',
            str(Path(config_dir).resolve()),
            'ssh-session',
            '--credential',
            principal.credential_id,
        ])
        escaped = command.replace('\\', '\\\\').replace('"', '\\"')
        return f'restrict,command="{escaped}" {line}'
    finally:
        await app.close()


async def forced_session(config_dir, credential_id):
    require_sshd_process()
    from msg.application import Application
    from msg.config import load_settings
    from msg.daemon import network_runtime

    app = Application(load_settings(config_dir))
    network_runtime(app.settings)
    await app.load()
    try:
        command = parse_command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        auth = SSHAuthenticator(app, credential_id)
        executor = app.new_executor(auth)
        async with app.metadata.transaction(write=False) as tx:
            principal = await ssh_principal(app, credential_id, tx)
        if command[0] in {'call', 'packet'}:
            from msg.core.codec import loads
            from msg.transports.packet import path_packet

            if command[0] == 'packet':
                packet = path_packet(command[1], 'j', app.settings.server.limits.max_request_bytes)
            else:
                packet = request_for(
                    command[1],
                    loads(command[2]),
                    app.settings.service_url,
                    subject=principal.subject,
                    expires_at=app.clock() + timedelta(seconds=120),
                    source='manual',
                )
            result = await executor.execute(packet)
            print(canonical(result_wire(result)).decode())
            return 0 if result.status in {'ok', 'accepted'} else 1
        from msg.extensions.repositories import NativeGitStore

        store = NativeGitStore(app)
        operation = 'git.refs' if command[0] == 'git-upload-pack' else 'git.receive'
        packet = request_for(
            operation,
            {'id': command[1]},
            app.settings.service_url,
            subject=principal.subject,
            expires_at=app.clock() + timedelta(seconds=120),
        )
        result = await executor.execute(packet)
        require(
            result.status in {'ok', 'accepted'},
            result.error.code if result.error else 'permission_denied',
        )
        rid = result.data['resource_id'] if operation == 'git.refs' else result.resources[0].id
        if operation == 'git.receive':
            from msg.extensions.ssh_git import receive_pack

            return await receive_pack(app, result.data['job_id'])
        process = await asyncio.create_subprocess_exec(
            'git',
            '--git-dir',
            str(store.path(rid)),
            '-c',
            'core.hooksPath=/dev/null',
            'upload-pack',
            '--strict',
            str(store.path(rid)),
            env=store.env,
        )
        try:
            return await asyncio.wait_for(process.wait(), 600)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    finally:
        await app.close()
