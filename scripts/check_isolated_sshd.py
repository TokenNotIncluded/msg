#!/usr/bin/env python3
"""Explicit local acceptance: disposable root-owned sshd, never system sshd.

Run with the installed project's Python as an ordinary user with sudo -n.
Requires PostgreSQL tools, OpenSSH and Git. No production configuration is read.
"""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile

from cryptography.hazmat.primitives import serialization

from msg.admin.diagnostics import temporary_postgres
from msg.admin.root import _provision, _approve_csr, _revoke
from msg.application import Application
from msg.config import write_example
from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.core.models import CertificateRequest, Signature
from msg.constants import ROOT_SUBJECT
from msg.security.certificates import csr_body
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer, subject_id


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, timeout=60, **kwargs)


async def acceptance():
    assert os.geteuid() != 0, 'Run as an ordinary service user, not root'
    assert run('sudo', '-n', 'true').returncode == 0, 'sudo -n is required'
    sshd = shutil.which('sshd')
    assert sshd and shutil.which('ssh') and shutil.which('ssh-keygen')
    checks = []
    with tempfile.TemporaryDirectory(prefix='msg-sshd-check-') as temporary, temporary_postgres() as dsn:
        folder = Path(temporary)
        app = Application(write_example(folder/'etc', folder/'data', 'https://sshd-check.invalid', postgres_dsn=dsn))
        assert app.settings.root_private_dir.is_relative_to(folder)
        server = None
        master = None
        privileged = None
        try:
            csr, root = await _provision(app, 'isolated-sshd-' + os.urandom(24).hex())
            await _approve_csr(app, csr, root, expected_digest=None, operator='isolated-sshd-check')
            # The service account must actually be unable to read root material.
            assert run('sudo', '-n', 'chown', '-R', '0:0', str(app.settings.root_private_dir)).returncode == 0
            key = Ed25519Signer.generate()
            uid = subject_id(key.public_key)

            async def call(name, args):
                packet = request_for(name, args, app.settings.service_url, signer=key, subject=uid,
                    expires_at=app.clock()+timedelta(seconds=120))
                return await app.executor.execute(packet)

            _, recipient = generate_age_key()
            packet = request_for('identity.register', {'handle': 'ssh-check', 'public_key': b64(key.public_key),
                'encryption_recipient': recipient}, app.settings.service_url, signer=key, subject=uid,
                contract_version=2, expires_at=app.clock()+timedelta(seconds=120))
            assert (await app.executor.execute(packet)).status == 'ok'
            repo = await call('git.create', {'parent': uid, 'name': 'code.git'})
            assert repo.status == 'ok'
            client_key = folder/'client-key'
            assert run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(client_key)).returncode == 0
            private = serialization.load_ssh_private_key(client_key.read_bytes(), password=None)
            signer = Ed25519Signer.from_bytes(private.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
            line = ' '.join(client_key.with_suffix('.pub').read_text().split()[:2])
            proof = signer.sign(canonical({'subject_id': uid, 'public_key': line}), purpose='ssh-key-add')
            grants = [g for g in app.base_grants() if g.capability in {'git.basic', 'discovery.basic'}]
            added = await call('identity.ssh_key_add', {'public_key': line, 'proof': wire(proof),
                                                      'ceiling': wire(grants)})
            assert added.status == 'ok'
            csr = CertificateRequest(resource_id='pending', applicant=uid, subject_id=uid,
                requested_issuer=ROOT_SUBJECT, public_key=signer.public_key, kind='capability',
                grants=tuple(grants), issuance=None, requested_ttl_seconds=600,
                target_service=app.settings.service_url, delegation_depth=0, authority_sources=(),
                request_digest='', possession_proof=Signature(key_id=signer.key_id,
                    algorithm='ed25519', value=b''))
            possession = signer.sign(canonical(csr_body(csr)), purpose='csr')
            requested = await call('cert.request', {'requested_issuer': ROOT_SUBJECT,
                'public_key': b64(signer.public_key), 'kind': 'capability', 'grants': wire(grants),
                'requested_ttl_seconds': 600, 'possession_proof': wire(possession)})
            assert requested.status == 'ok'
            certificate = await _approve_csr(app, requested.data['csr_id'], root,
                expected_digest=requested.data['request_digest'], operator='isolated-sshd-check')
            attached = await call('identity.ssh_certificates',
                {'key_id': signer.key_id, 'certificates': [certificate.resource_id]})
            assert attached.status == 'ok'
            host = folder/'host-key'
            assert run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host)).returncode == 0
            privileged = Path(run('sudo', '-n', 'mktemp', '-d', '/run/msg-sshd-check-XXXXXXXX', check=True).stdout.decode().strip())
            assert privileged.parent == Path('/run') and privileged.name.startswith('msg-sshd-check-')
            assert run('sudo', '-n', 'chmod', '755', str(privileged)).returncode == 0
            wrapper = folder/'authorized'
            wrapper.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-m', 'msg.daemon',
                '--config-dir', str(folder/'etc'), 'ssh-authorized-key']) + ' "$@"\n')
            assert run('sudo', '-n', 'install', '-m', '755', str(wrapper), str(privileged/'authorized')).returncode == 0
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            username = pwd.getpwuid(os.geteuid()).pw_name
            pidfile = folder/'sshd.pid'
            config = folder/'sshd.conf'
            config.write_text(f'''ListenAddress 127.0.0.1
Port {port}
HostKey {host}
PidFile {pidfile}
AllowUsers {username}
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
UsePAM no
AuthorizedKeysFile none
AuthorizedKeysCommand {privileged}/authorized %t %k
AuthorizedKeysCommandUser {username}
PermitTTY no
DisableForwarding yes
PermitUserRC no
PermitUserEnvironment no
LogLevel ERROR
''')
            assert run('sudo', '-n', sshd, '-t', '-f', str(config)).returncode == 0
            server = subprocess.Popen(['sudo', '-n', sshd, '-D', '-e', '-f', str(config)],
                                      stdout=subprocess.DEVNULL, stderr=(folder/'sshd.log').open('wb'))
            for _ in range(100):
                if pidfile.exists():
                    break
                assert server.poll() is None, 'isolated sshd exited'
                await asyncio.sleep(.05)
            assert pidfile.exists()
            host_fields = host.with_suffix('.pub').read_text().split()[:2]
            known = folder/'known-hosts'
            known.write_text(f'[127.0.0.1]:{port} ' + ' '.join(host_fields) + '\n')
            ssh = ['ssh', '-F', '/dev/null', '-p', str(port), '-i', str(client_key),
                   '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', f'UserKnownHostsFile={known}', '-o', 'ConnectTimeout=5']
            target = f'{username}@127.0.0.1'
            read = await asyncio.to_thread(run, *ssh, target, 'msg call discovery.get \'{"id":"/main"}\'')
            assert read.returncode == 0, (read.stderr.decode(), (folder/'sshd.log').read_text())
            assert json.loads(read.stdout)['status'] == 'ok'
            checks.append('real_authorized_keys_and_forced_command')
            for command in ('id', 'sh', 'git-upload-pack /etc/passwd'):
                denied = await asyncio.to_thread(run, *ssh, target, command)
                assert denied.returncode != 0
            checks.append('shell_and_path_escape_denied')
            forwarding = await asyncio.to_thread(run, *ssh, '-o', 'ExitOnForwardFailure=yes',
                '-R', '0:127.0.0.1:1', target, 'msg call discovery.get \'{"id":"/main"}\'')
            assert forwarding.returncode != 0 and b'remote port forwarding failed' in forwarding.stderr
            checks.append('remote_forwarding_denied')
            work = folder/'work'; work.mkdir()
            def git(*args):
                return run('git', '-C', str(work), *args)
            assert git('init', '--initial-branch=main').returncode == 0
            assert git('config', 'user.name', 'Isolated').returncode == 0
            assert git('config', 'user.email', 'isolated@example.invalid').returncode == 0
            (work/'README').write_text('real SSH Git acceptance\n')
            assert git('add', '.').returncode == 0
            assert git('commit', '-m', 'isolated').returncode == 0
            assert git('branch', 'second').returncode == 0
            remote = f'{target}:/@ssh-check/code.git'
            command = ['git', '-C', str(work), '-c', 'core.sshCommand='+shlex.join(ssh)]
            pushed = await asyncio.to_thread(run, *command, 'push', '--atomic', remote, 'main', 'second')
            assert pushed.returncode == 0, pushed.stderr.decode()
            fetched = await asyncio.to_thread(run, *command, 'ls-remote', remote)
            assert fetched.returncode == 0 and b'refs/heads/main' in fetched.stdout and b'refs/heads/second' in fetched.stdout
            clone = folder/'clone'
            cloned = await asyncio.to_thread(run, *command, 'clone', remote, str(clone))
            assert cloned.returncode == 0 and (clone/'README').read_bytes() == (work/'README').read_bytes()
            checks.append('real_ssh_atomic_multi_ref_push_and_fetch')
            control = folder/'control'
            master = subprocess.Popen([*ssh, '-M', '-S', str(control), '-N', target],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(200):
                if control.exists():
                    break
                assert master.poll() is None, 'SSH master connection failed'
                await asyncio.sleep(.05)
            assert control.exists()
            await _revoke(app, certificate.resource_id, root, reason='isolated test',
                          operator='isolated-sshd-check')
            cert_denied = await asyncio.to_thread(run, *ssh, '-S', str(control), target,
                                                 'msg call discovery.get \'{"id":"/main"}\'')
            assert cert_denied.returncode != 0 and b'certificate_revoked' in cert_denied.stdout + cert_denied.stderr
            new_denied = await asyncio.to_thread(run, *ssh, target,
                                                'msg call discovery.get \'{"id":"/main"}\'')
            assert new_denied.returncode == 255
            checks.append('revoked_certificate_denies_new_and_existing_authenticated_connections')
            # Explicitly detach the revoked optional certificate; never unrevoke it.
            detached = await call('identity.ssh_certificates', {'key_id': signer.key_id, 'certificates': []})
            assert detached.status == 'ok'
            revoked = await call('identity.ssh_key_revoke', {'key_id': signer.key_id})
            assert revoked.status == 'ok'
            reused = await asyncio.to_thread(run, *ssh, '-S', str(control), target,
                                            'msg call discovery.get \'{"id":"/main"}\'')
            assert reused.returncode != 0 and b'credential_revoked' in reused.stdout + reused.stderr
            checks.append('revocation_rechecked_inside_already_authenticated_connection')
            denied = await asyncio.to_thread(run, *ssh, target, 'msg call discovery.get \'{"id":"/main"}\'')
            assert denied.returncode == 255 and b'Permission denied' in denied.stderr
            denied_git = await asyncio.to_thread(run, *command, 'ls-remote', remote)
            assert denied_git.returncode != 0
            checks.append('revoked_registered_ssh_key_denies_new_sessions_and_git')
        finally:
            if master is not None:
                master.terminate()
                master.wait(timeout=15)
            if server is not None:
                if pidfile.exists():
                    pid = int(pidfile.read_text().strip())
                    run('sudo', '-n', 'kill', '-TERM', str(pid))
                server.wait(timeout=15)
            await app.close()
            # These two paths were created in this function; no installed paths.
            run('sudo', '-n', 'rm', '-rf', '--', str(app.settings.root_private_dir))
            if privileged is not None:
                run('sudo', '-n', 'rm', '-rf', '--', str(privileged))
    return {'checks': checks, 'temporary_installation_removed': not folder.exists()}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(acceptance()), indent=2))
