"""Local identity, automatic signing, resumable file exchange, and bounded retries."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from msg.atomic_file import durable_write
from msg.client_tokens import read_journal, remove_journal, token_operation
from msg.core.codec import b64, canonical, decode, digest, loads, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import ResourceRef
from msg.core.requests import request_for
from msg.paths import ClientPaths
from msg.security.age_keys import generate_age_key, recipient_from_identity
from msg.security.crypto import Ed25519Signer, subject_id
from msg.security.custodial_protocol import client_upgrade_proof
from msg.transports.client import GraphQLTransport, HTTPTransport, MCPHTTPTransport


def hash_file(path):
    hashed = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as stream:
        while data := stream.read(65536):
            size += len(data)
            hashed.update(data)
    return size, 'sha256:' + hashed.hexdigest()


class ClientState:
    """Owned files only; a failed registration never loses its private key."""

    def __init__(self, directory=None, *, server=None, profile=None, migrate_from=None, paths=None):
        require(
            migrate_from is None or (Path(migrate_from).expanduser() / 'client.json').is_file(),
            'client_migration_source_missing',
        )
        self.paths = paths or ClientPaths.discover(directory, profile=profile)
        self.paths.prepare()
        if not self.paths.portable:
            legacy = migrate_from or (self.paths.config if profile is None else None)
            if legacy is not None and (Path(legacy) / 'client.json').exists():
                self.paths.migrate(legacy)
        self.directory = self.paths.state
        self.path = self.file('client.json')
        self.key_path = self.file('identity.key')
        self.age_key_path = self.file('encryption.agekey')
        self.pending_path = self.file('registration.json')
        if self.path.exists() or self.path.is_symlink():
            require(
                self.path.is_file()
                and not self.path.is_symlink()
                and self.path.stat().st_uid == os.geteuid()
                and self.path.stat().st_mode & 0o077 == 0,
                'unsafe_client_state_permissions',
            )
        self.data = loads(self.path.read_bytes()) if self.path.exists() else {'version': 1}
        require(self.data.get('version') == 1, 'unknown_client_state_version')
        require(
            server is None or self.data.get('server', server) == server.rstrip('/'),
            'client_server_mismatch',
        )
        self.server = self.data.get('server', server or 'https://msg.lmm.best').rstrip('/')
        self.data['server'] = self.server
        self.signer = None
        self.encryption_recipient = None
        if self.key_path.exists() or self.key_path.is_symlink():
            require(
                self.key_path.is_file()
                and not self.key_path.is_symlink()
                and self.key_path.stat().st_uid == os.geteuid()
                and self.key_path.stat().st_mode & 0o077 == 0,
                'unsafe_client_key_permissions',
            )
            self.signer = Ed25519Signer.from_bytes(self.key_path.read_bytes())
        if self.age_key_path.exists() or self.age_key_path.is_symlink():
            require(
                self.age_key_path.is_file()
                and not self.age_key_path.is_symlink()
                and self.age_key_path.stat().st_uid == os.geteuid()
                and self.age_key_path.stat().st_mode & 0o077 == 0,
                'unsafe_client_key_permissions',
            )
            self.encryption_recipient = recipient_from_identity(
                self.age_key_path.read_text().strip()
            )
        self._save()

    def file(self, name):
        return self.paths.file(name)

    @property
    def cache_directory(self):
        return self.paths.cache

    @property
    def temporary_directory(self):
        # TemporaryDirectory creates a private, unique child. If the session
        # runtime directory is unavailable, Python honors TMPDIR/TEMP/TMP.
        return self.paths.temporary_parent()

    @property
    def subject(self):
        return self.data.get('subject_id')

    @property
    def certificates(self):
        return tuple(self.data.get('certificates', ()))

    @property
    def token(self):
        saved = self.data.get('token')
        return (saved['credential_id'], unb64(saved['value'])) if saved else None

    def _save(self):
        durable_write(self.path, canonical(self.data), mode=0o600)

    def save_signer(self, signer):
        require(not self.key_path.exists(), 'identity_key_already_exists')
        durable_write(self.key_path, signer.private_bytes(), mode=0o600)
        self.signer = signer

    def ensure_encryption_key(self):
        if self.encryption_recipient is None:
            require(not self.age_key_path.exists(), 'invalid_age_identity')
            identity, recipient = generate_age_key()
            durable_write(self.age_key_path, (identity + '\n').encode(), mode=0o600)
            self.encryption_recipient = recipient
        return self.encryption_recipient

    def accept_identity(self, result):
        require(result.status == 'ok', 'identity_operation_failed')
        if result.data.get('encryption_recipient'):
            if result.operation == 'identity.custodial_create':
                require(
                    self.signer is None and self.encryption_recipient is None,
                    'custodial_client_key_conflict',
                )
                self.data['custodial_encryption_recipient'] = result.data['encryption_recipient']
            else:
                require(
                    result.data['encryption_recipient'] == self.encryption_recipient,
                    'encryption_recipient_mismatch',
                )
                self.data.pop('custodial_encryption_recipient', None)
        self.data['subject_id'] = result.subject
        if result.data.get('certificate_id'):
            self.data['certificates'] = [result.data['certificate_id']]
        if result.data.get('token'):
            self.data['token'] = {
                'credential_id': result.data['credential_id'],
                'value': result.data['token'],
                'expires_at': result.data['expires_at'],
            }
        elif self.signer is not None:
            self.data.pop('token', None)
        self._save()


class MsgClient:
    def __init__(self, state, transport, *, clock=None, retries=2):
        self.state, self.transport = state, transport
        self.clock = clock or (lambda: datetime.now(UTC))
        self.retries = retries
        self.signer_override = None
        require(state.server == transport.server, 'client_server_mismatch')

    def prepare(
        self,
        operation,
        arguments,
        *,
        expected=(),
        request_id=None,
        return_fields=(),
        subject=None,
        signer=None,
        certificates=None,
        anonymous=False,
        contract_version=1,
        expires_at=None,
    ):
        require(not operation.startswith('root.'), 'local_only')
        signer = signer or self.signer_override
        if (self.state.file('token-rotation.json')).exists():
            require(operation == 'identity.token_rotate', 'token_rotation_pending')
        selected_signer = signer or self.state.signer
        selected_subject = subject or self.state.subject
        # An explicit temporary token remains authoritative until upgrade succeeds.
        token = self.state.token if signer is None else None
        if token is None and signer is None and self.state.data.get('api_key'):
            saved = self.state.data['api_key']
            token = (saved['credential_id'], unb64(saved['value']))
        if (
            not anonymous
            and signer is None
            and operation not in {'identity.oauth_request', 'identity.oauth_approve'}
        ):
            from msg.client_oauth import read_session

            session = read_session(self.state)
            if session is not None:
                credential, _, encoded = session['access_token'].partition('.')
                token = (credential, unb64(encoded, limit=32))
                selected_subject = selected_subject or session['subject_id']
        if token:
            selected_signer = None
        return request_for(
            operation,
            arguments,
            self.state.server,
            subject=None if anonymous else selected_subject,
            signer=None if anonymous else selected_signer,
            token=None if anonymous else token,
            certificates=()
            if anonymous
            else (self.state.certificates if certificates is None else certificates),
            request_id=request_id,
            expires_at=expires_at or self.clock() + timedelta(seconds=180),
            source='msg',
            expected=expected,
            return_fields=return_fields,
            contract_version=contract_version,
        )

    async def send(self, packet):
        result = None
        for attempt in range(self.retries + 1):
            try:
                result = await self.transport.call(packet)
                if (
                    result.status != 'error'
                    or not result.error.retryable
                    or attempt == self.retries
                ):
                    return result
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.retries:
                    # Transport failure is not evidence of a rollback. The caller
                    # must retry this request ID, not invent another operation.
                    raise Failure(
                        'transport_uncertain',
                        retryable=True,
                        details={'request_id': packet.request_id},
                    ) from exc
            except Failure as exc:
                if not exc.retryable or attempt == self.retries:
                    raise
            await asyncio.sleep(min(2, 0.2 * (2**attempt)))
        return result

    async def call(self, operation, arguments=None, **kwargs):
        if (
            not kwargs.get('anonymous')
            and kwargs.get('signer') is None
            and self.signer_override is None
        ):
            from msg.client_oauth import read_session, refresh

            if read_session(self.state) is not None:
                await refresh(self)
        return await self.send(self.prepare(operation, arguments or {}, **kwargs))

    async def _website_generation(self, website):
        meta = self.checked(await self.call('discovery.get', {'id': website, 'view': 'meta'}))
        require(meta.data.get('type') == 'website', 'not_a_website')
        return meta.data['id'], meta.data['generation']

    @staticmethod
    def _hosting_entries(entries):
        require(type(entries) is list and bool(entries), 'hosting_entries_required')
        names = set()
        validated = []
        for item in entries:
            require(type(item) is dict and set(item) == {'path', 'source'}, 'invalid_hosting_entry')
            path = item['path']
            source = item['source']
            require(
                type(path) is str
                and 0 < len(path) <= 2048
                and all(p not in {'', '.', '..'} and not p.startswith('.') for p in path.split('/'))
                and '\\' not in path
                and all(32 <= ord(c) < 127 for c in path)
                and path not in names,
                'invalid_hosting_path',
            )
            require(
                type(source) is dict
                and type(source.get('id')) is str
                and type(source.get('revision')) is str
                and bool(source['revision'])
                and set(source) == {'id', 'revision'},
                'source_revision_required',
            )
            names.add(path)
            validated.append({'path': path, 'source': source})
        return validated

    async def hosting_preview(self, website, entries):
        require(
            self.state.signer is not None and self.state.token is None, 'signing_identity_required'
        )
        rid, generation = await self._website_generation(website)
        return await self.call(
            'hosting.preview',
            {'id': rid, 'entries': self._hosting_entries(entries)},
            expected=((rid, generation),),
        )

    async def hosting_deploy(self, website, entries):
        require(
            self.state.signer is not None and self.state.token is None, 'signing_identity_required'
        )
        rid, generation = await self._website_generation(website)
        return await self.call(
            'hosting.deploy',
            {'id': rid, 'entries': self._hosting_entries(entries)},
            expected=((rid, generation),),
        )

    async def hosting_activate(self, website, revision):
        require(
            self.state.signer is not None and self.state.token is None, 'signing_identity_required'
        )
        require(type(revision) is str and bool(revision), 'revision_required')
        rid, generation = await self._website_generation(website)
        return await self.call(
            'hosting.activate', {'id': rid, 'revision': revision}, expected=((rid, generation),)
        )

    async def hosting_preview_get(
        self, site_path, candidate_id, file_path='index.html', *, max_bytes=1048576
    ):
        """Fetch candidate bytes with a header-bound proof, never a shareable URL."""
        require(
            self.state.signer is not None and self.state.token is None, 'signing_identity_required'
        )
        require(type(self.transport) is HTTPTransport, 'hosting_http_transport_required')
        require(
            type(site_path) is str
            and re.fullmatch(r'/[@&][A-Za-z0-9_-]+/[A-Za-z0-9_-]+', site_path),
            'invalid_website_path',
        )
        require(
            type(candidate_id) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', candidate_id),
            'invalid_candidate_id',
        )
        require(
            type(file_path) is str
            and file_path
            and len(file_path) <= 2048
            and all(
                p not in {'', '.', '..'} and not p.startswith('.') for p in file_path.split('/')
            )
            and all(32 <= ord(c) < 127 for c in file_path)
            and '\\' not in file_path
            and '%' not in file_path
            and '?' not in file_path
            and '#' not in file_path,
            'invalid_hosting_path',
        )
        require(type(max_bytes) is int and 0 < max_bytes <= 10485760, 'invalid_preview_limit')
        packet = self.prepare('discovery.raw', {'id': candidate_id})
        header = b64(canonical(wire(packet)))
        path = site_path + '/_preview/' + candidate_id + '/' + file_path
        async with self.transport.http.stream(
            'GET',
            self.state.server + path,
            headers={'X-Msg-Request': header},
            follow_redirects=False,
        ) as response:
            require(not response.is_redirect, 'redirect_not_allowed')
            if response.status_code != 200:
                code = (
                    'preview_permission_denied'
                    if response.status_code in {401, 403}
                    else 'preview_not_found'
                    if response.status_code == 404
                    else 'preview_request_failed'
                )
                raise Failure(code, details={'status': response.status_code})
            csp = response.headers.get('content-security-policy', '').lower()
            require(
                'sandbox' in csp.split(';')[0].split()
                and 'allow-scripts' not in csp
                and 'allow-same-origin' not in csp
                and response.headers.get('x-content-type-options') == 'nosniff',
                'unsafe_preview_response',
            )
            data = bytearray()
            async for piece in response.aiter_bytes():
                require(len(data) + len(piece) <= max_bytes, 'preview_too_large')
                data.extend(piece)
            return bytes(data), {
                'media_type': response.headers.get('content-type', 'application/octet-stream'),
                'content_disposition': response.headers.get('content-disposition'),
                'etag': response.headers.get('etag'),
            }

    def _require_token_secret_transport(self):
        # These envelopes contain a recovery secret. A path transport would put
        # it in URL history and access logs, including when it compresses paths.
        require(
            type(self.transport) in {HTTPTransport, GraphQLTransport, MCPHTTPTransport},
            'secure_channel_required',
        )
        host = urlsplit(self.state.server).hostname
        require(
            self.state.server.startswith('https://')
            or host in {'testserver', 'localhost', '127.0.0.1', '::1'},
            'secure_channel_required',
        )

    async def _send_token_secret(self, packet):
        self._require_token_secret_transport()
        return await self.send(packet)

    def _token_journal(self):
        return read_journal(self.state)

    @staticmethod
    def _token_claim(nonce, request_id, operation, subject=None):
        if operation in {'identity.temporary', 'identity.custodial_create'}:
            prefix = 'u_tmp_' if operation == 'identity.temporary' else 'u_cust_'
            subject = prefix + hashlib.sha256(unb64(nonce, limit=64)).hexdigest()[:32]
            credential = 't_' + subject[2:]
        else:
            require(subject is not None, 'token_subject_required')
            credential = 't_' + digest((request_id, subject))[7:39]
        return subject, credential

    def _new_token_journal(self, path, operation, *, handle=None, contract_version=2):
        require(self._token_journal() == (None, None), 'token_operation_pending')
        nonce = b64(os.urandom(32))
        recovery_secret = b64(os.urandom(32))
        request_id = uuid4().hex
        subject, credential = self._token_claim(nonce, request_id, operation, self.state.subject)
        pending = {
            'server': self.state.server,
            'contract_version': contract_version,
            'operation': operation,
            'nonce': nonce,
            'recovery_secret': recovery_secret,
            'request_id': request_id,
            'expires_at': wire(self.clock() + timedelta(seconds=180)),
            'subject_id': subject,
            'credential_id': credential,
        }
        if operation == 'identity.temporary':
            pending.update(
                public_key=b64(self.state.signer.public_key),
                encryption_recipient=self.state.encryption_recipient,
            )
        if handle is not None:
            pending['handle'] = handle
        durable_write(path, canonical(pending), mode=0o600)
        return pending

    def _existing_token_journal(self, path, operation, *, handle=None, contract_version=2):
        existing, pending = self._token_journal()
        if pending is None:
            return self._new_token_journal(
                path, operation, handle=handle, contract_version=contract_version
            )
        require(
            existing == path
            and pending['operation'] == operation
            and (handle is None or pending.get('handle') == handle),
            'token_operation_pending',
        )
        require(
            pending['contract_version'] == contract_version,
            'legacy_token_journal_requires_manual_resolution',
        )
        return pending

    def _finish_token(self, path, pending, result):
        if result.status == 'ok':
            require(
                result.data['credential_id'] == pending['credential_id']
                and result.data['subject_id'] == pending['subject_id']
                and bool(result.data.get('token')),
                'token_claim_mismatch',
            )
            if pending['operation'] == 'identity.token_create':
                from msg.client_api_keys import accept

                accept(self.state, result)
            else:
                self.state.accept_identity(result)
            remove_journal(path)
        return result

    @staticmethod
    def checked(result):
        if result.status == 'error':
            raise Failure(
                result.error.code,
                result.error.field_path,
                retryable=result.error.retryable,
                details=dict(result.data or {}),
            )
        return result

    async def renew_certificate(self):
        require(
            self.state.signer is not None and self.state.token is None, 'signing_identity_required'
        )
        # Renewal must remain possible after expiry or a root trust rotation.
        result = await self.call('identity.certificate_renew', certificates=())
        if result.status == 'ok':
            self.state.data['certificates'] = [result.data['certificate_id']]
            self.state._save()
        return result

    async def register(self, handle):
        require(self.state.subject is None, 'identity_already_configured')
        if self.state.pending_path.exists():
            pending = loads(self.state.pending_path.read_bytes())
            require(pending['handle'] == handle, 'registration_pending_for_other_handle')
        else:
            if self.state.signer is None:
                self.state.save_signer(Ed25519Signer.generate())
            self.state.ensure_encryption_key()
            pending = {'handle': handle, 'request_id': uuid4().hex}
            durable_write(self.state.pending_path, canonical(pending), mode=0o600)
        signer = self.state.signer
        recipient = self.state.ensure_encryption_key()
        result = await self.call(
            'identity.register',
            {
                'handle': handle,
                'public_key': b64(signer.public_key),
                'encryption_recipient': recipient,
            },
            contract_version=2,
            subject=subject_id(signer.public_key),
            signer=signer,
            certificates=(),
            request_id=pending['request_id'],
        )
        if result.status == 'ok':
            self.state.accept_identity(result)
            self.state.pending_path.unlink(missing_ok=True)
        return result

    @token_operation
    async def temporary(self):
        require(self.state.subject is None, 'identity_already_configured')
        self._require_token_secret_transport()
        if self.state.signer is None:
            self.state.save_signer(Ed25519Signer.generate())
        signer = self.state.signer
        recipient = self.state.ensure_encryption_key()
        pending = self.state.file('temporary.json')
        data = self._existing_token_journal(pending, 'identity.temporary', contract_version=3)
        proof = signer.sign(
            canonical({
                'subject_id': data['subject_id'],
                'nonce': data['nonce'],
                'public_key': b64(signer.public_key),
                'encryption_recipient': recipient,
                'request_id': data['request_id'],
            }),
            purpose='temporary-key-possession-v1',
        )
        packet = self.prepare(
            'identity.temporary',
            {
                'nonce': data['nonce'],
                'recovery_secret': data['recovery_secret'],
                'public_key': b64(signer.public_key),
                'encryption_recipient': recipient,
                'possession_proof': wire(proof),
            },
            request_id=data['request_id'],
            anonymous=True,
            contract_version=3,
            expires_at=self.clock() + timedelta(seconds=180),
        )
        return self._finish_token(pending, data, await self._send_token_secret(packet))

    @token_operation
    async def custodial(self, handle):
        require(
            self.state.subject is None
            and self.state.signer is None
            and self.state.encryption_recipient is None,
            'identity_already_configured',
        )
        self._require_token_secret_transport()
        pending = self.state.file('custodial-bootstrap.json')
        data = self._existing_token_journal(pending, 'identity.custodial_create', handle=handle)
        packet = self.prepare(
            'identity.custodial_create',
            {'handle': handle, 'nonce': data['nonce'], 'recovery_secret': data['recovery_secret']},
            request_id=data['request_id'],
            anonymous=True,
            contract_version=2,
            expires_at=self.clock() + timedelta(seconds=180),
        )
        return self._finish_token(pending, data, await self._send_token_secret(packet))

    @token_operation
    async def rotate_token(self):
        require(self.state.token is not None, 'token_required')
        self._require_token_secret_transport()
        pending = self.state.file('token-rotation.json')
        data = self._existing_token_journal(pending, 'identity.token_rotate')
        packet = self.prepare(
            'identity.token_rotate',
            {'nonce': data['nonce'], 'recovery_secret': data['recovery_secret']},
            request_id=data['request_id'],
            contract_version=2,
            expires_at=self.clock() + timedelta(seconds=180),
        )
        return self._finish_token(pending, data, await self._send_token_secret(packet))

    @token_operation
    async def recover_token(self):
        self._require_token_secret_transport()
        path, pending = self._token_journal()
        require(path is not None, 'token_recovery_not_pending')
        # A crash after writing client.json but before unlinking the journal is
        # complete locally. Never revoke that already accepted credential.
        accepted = {pending['credential_id'], (pending.get('recovery') or {}).get('credential_id')}
        saved_api = (
            self.state.data.get('api_key')
            if pending['operation'] == 'identity.token_create'
            else None
        )
        if (self.state.token and self.state.token[0] in accepted) or (
            saved_api and saved_api['credential_id'] in accepted
        ):
            remove_journal(path)
            raise Failure('token_recovery_not_pending')
        recovery = pending.get('recovery')
        if recovery is None:
            nonce = b64(os.urandom(32))
            request_id = uuid4().hex
            _, credential = self._token_claim(
                nonce, request_id, 'identity.token_recover', pending['subject_id']
            )
            recovery = {
                'nonce': nonce,
                'new_recovery_secret': b64(os.urandom(32)),
                'request_id': request_id,
                'credential_id': credential,
                'expires_at': wire(self.clock() + timedelta(seconds=180)),
            }
            pending['recovery'] = recovery
            durable_write(path, canonical(pending), mode=0o600)
        args = {
            'credential_id': pending['credential_id'],
            'original_request_id': pending['request_id'],
            'recovery_secret': pending['recovery_secret'],
            'nonce': recovery['nonce'],
            'new_recovery_secret': recovery['new_recovery_secret'],
        }
        packet = request_for(
            'identity.token_recover',
            args,
            self.state.server,
            subject=pending['subject_id'],
            request_id=recovery['request_id'],
            expires_at=self.clock() + timedelta(seconds=180),
            source='msg',
        )
        result = await self._send_token_secret(packet)
        if result.status == 'ok':
            require(
                result.data['credential_id'] == recovery['credential_id']
                and result.data['subject_id'] == pending['subject_id']
                and result.data['previous_credential'] == pending['credential_id']
                and bool(result.data.get('token')),
                'token_claim_mismatch',
            )
            if pending['operation'] == 'identity.token_create':
                from msg.client_api_keys import accept

                accept(self.state, result)
            else:
                self.state.accept_identity(result)
            remove_journal(path)
        elif result.error.code == 'token_delivery_unavailable':
            # Claim committed but its response may have vanished. A further
            # explicit recovery consumes the newly bound secret, never @1.
            pending.update(
                credential_id=recovery['credential_id'],
                request_id=recovery['request_id'],
                recovery_secret=recovery['new_recovery_secret'],
            )
            pending.pop('recovery')
            durable_write(path, canonical(pending), mode=0o600)
        return result

    async def upgrade(self, handle=None):
        from msg.client_upgrade import upgrade_identity

        return await upgrade_identity(self, handle)

    async def upgrade_custodial(self, handle, *, external_ciphertexts_migrated: bool):
        from msg.client_upgrade import locked_state

        self._require_token_secret_transport()
        with locked_state(self.state):
            require(
                not any(
                    (self.state.file(name)).exists() or (self.state.file(name)).is_symlink()
                    for name in (
                        'identity-upgrade.json',
                        'temporary.json',
                        'custodial-bootstrap.json',
                        'token-rotation.json',
                    )
                ),
                'identity_recovery_pending',
            )
            return await self._upgrade_custodial(
                handle, external_ciphertexts_migrated=external_ciphertexts_migrated
            )

    async def _upgrade_custodial(self, handle, *, external_ciphertexts_migrated: bool):
        require(type(external_ciphertexts_migrated) is bool, 'migration_statement_required')
        require(
            self.state.subject is not None and self.state.token is not None,
            'custodial_token_required',
        )
        journal = self.state.file('custodial-upgrade.json')
        if self.state.signer is None:
            self.state.save_signer(Ed25519Signer.generate())
        recipient = self.state.ensure_encryption_key()
        public = b64(self.state.signer.public_key)
        if journal.exists():
            pending = loads(journal.read_bytes())
            require(
                pending['handle'] == handle
                and pending['public_key'] == public
                and pending['encryption_recipient'] == recipient,
                'custodial_upgrade_journal_mismatch',
            )
        else:
            pending = {
                'handle': handle,
                'public_key': public,
                'encryption_recipient': recipient,
                'server': self.state.server,
                'subject_id': self.state.subject,
                'start_request_id': uuid4().hex,
                'finish_request_id': uuid4().hex,
                'challenge': None,
            }
            durable_write(journal, canonical(pending), mode=0o600)
        if pending['challenge'] is None:
            signed = {
                'subject_id': self.state.subject,
                'handle': handle,
                'public_key': public,
                'encryption_recipient': recipient,
                'request_id': pending['start_request_id'],
            }
            proof = self.state.signer.sign(canonical(signed), purpose='custodial-upgrade-start')
            started = await self.call(
                'identity.custodial_upgrade_start',
                {
                    'handle': handle,
                    'public_key': public,
                    'encryption_recipient': recipient,
                    'possession_proof': wire(proof),
                },
                request_id=pending['start_request_id'],
            )
            if started.status != 'ok':
                return started
            pending['challenge'] = dict(started.data)
            durable_write(journal, canonical(pending), mode=0o600)
        challenge = pending['challenge']
        if pending.get('status') == 'pending_rewrap':
            return await self.call(
                'identity.custodial_upgrade_inventory', {'challenge_id': challenge['challenge_id']}
            )
        age_identity = self.state.age_key_path.read_text().strip()
        require(recipient_from_identity(age_identity) == recipient, 'encryption_identity_mismatch')
        age_proof = client_upgrade_proof(age_identity, challenge)
        acknowledgement = {
            'subject_id': self.state.subject,
            'challenge_id': challenge['challenge_id'],
            'age_proof': age_proof,
            'external_ciphertexts_migrated': external_ciphertexts_migrated,
            'request_id': pending['finish_request_id'],
        }
        signed_ack = self.state.signer.sign(
            canonical(acknowledgement), purpose='custodial-upgrade-finish'
        )

        async def recover_with_new_key():
            return await self.call(
                'identity.custodial_upgrade_result',
                {'challenge_id': challenge['challenge_id']},
                signer=self.state.signer,
                subject=self.state.subject,
                certificates=(),
            )

        try:
            result = await self.call(
                'identity.custodial_upgrade_finish',
                {
                    'challenge_id': challenge['challenge_id'],
                    'age_proof': age_proof,
                    'external_ciphertexts_migrated': external_ciphertexts_migrated,
                    'migration_ack': wire(signed_ack),
                },
                request_id=pending['finish_request_id'],
            )
        except Failure as exc:
            if exc.code != 'transport_uncertain':
                raise
            recovered = await recover_with_new_key()
            if recovered.status != 'ok':
                raise
            result = recovered
        if result.status == 'error' and result.error.code in {
            'credential_expired',
            'credential_revoked',
            'request_expired',
            'idempotency_conflict',
        }:
            recovered = await recover_with_new_key()
            if recovered.status == 'ok':
                result = recovered
        if result.status == 'ok' and result.data['status'] == 'completed':
            self.state.accept_identity(result)
            journal.unlink(missing_ok=True)
        elif result.status == 'ok' and result.data['status'] == 'pending_rewrap':
            # Preserve the bound challenge and new private keys for the
            # per-entry rewrap inventory and signed client acknowledgements.
            # A later finish attempt has its own request ID; replaying this
            # one would only retrieve the original pending result.
            pending['status'] = 'pending_rewrap'
            pending['finish_request_id'] = uuid4().hex
            durable_write(journal, canonical(pending), mode=0o600)
        return result

    async def rotate_encryption_key(self):
        require(
            self.state.subject is not None
            and self.state.signer is not None
            and self.state.token is None,
            'signing_identity_required',
        )
        journal = self.state.file('encryption-rotation.json')
        pending_key = self.state.file('encryption.pending.agekey')
        if journal.exists():
            pending = loads(journal.read_bytes())
            require(
                pending_key.is_file()
                and not pending_key.is_symlink()
                and pending_key.stat().st_uid == os.geteuid()
                and pending_key.stat().st_mode & 0o077 == 0,
                'unsafe_client_key_permissions',
            )
            require(
                recipient_from_identity(pending_key.read_text().strip()) == pending['recipient'],
                'encryption_rotation_mismatch',
            )
        else:
            require(self.state.encryption_recipient is not None, 'encryption_key_not_found')
            identity, recipient = generate_age_key()
            durable_write(pending_key, (identity + '\n').encode(), mode=0o600)
            pending = {'recipient': recipient, 'request_id': uuid4().hex}
            durable_write(journal, canonical(pending), mode=0o600)
        result = await self.call(
            'identity.encryption_key_rotate',
            {'encryption_recipient': pending['recipient']},
            request_id=pending['request_id'],
        )
        if result.status == 'ok':
            require(
                result.data['recipient'] == pending['recipient'], 'encryption_rotation_mismatch'
            )
            previous = result.data['previous_key_id']
            history = self.state.file('encryption-' + previous + '.agekey')
            if (
                self.state.age_key_path.exists()
                and self.state.encryption_recipient != pending['recipient']
            ):
                require(not history.exists(), 'encryption_history_conflict')
                os.replace(self.state.age_key_path, history)
            if not self.state.age_key_path.exists():
                os.replace(pending_key, self.state.age_key_path)
            self.state.encryption_recipient = pending['recipient']
            pending_key.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
        return result

    async def upload(
        self,
        path,
        *,
        transfer_id=None,
        part_bytes=65536,
        media_type='application/octet-stream',
        target=None,
    ):
        require(type(part_bytes) is int and part_bytes > 0, 'invalid_part_bytes')
        path = Path(path)
        size, hashed = await asyncio.to_thread(hash_file, path)
        journal = self.state.file('upload-' + digest((str(path.resolve()), hashed))[7:39] + '.json')
        saved = loads(journal.read_bytes()) if journal.exists() else None
        if transfer_id is None and saved:
            transfer_id = saved.get('transfer_id')
        if transfer_id is None:
            limits = await self.transport.discover()
            arguments = {
                'direction': 'upload',
                'size': size,
                'digest': hashed,
                'media_type': media_type,
                'requested_part_bytes': part_bytes,
                'max_request_bytes': limits.max_request_bytes,
                'max_response_bytes': limits.max_response_bytes,
                'max_path_bytes': limits.max_path_bytes,
            }
            if target:
                arguments['target'] = wire(
                    target if isinstance(target, ResourceRef) else ResourceRef(id=target)
                )
            saved = saved or {'request_id': uuid4().hex, 'size': size, 'digest': hashed}
            durable_write(journal, canonical(saved), mode=0o600)
            result = self.checked(
                await self.call('transfer.open', arguments, request_id=saved['request_id'])
            )
            transfer_id = result.data['transfer_id']
            part_bytes = result.data['part_bytes']
            saved.update(transfer_id=transfer_id, part_bytes=part_bytes)
            durable_write(journal, canonical(saved), mode=0o600)
        elif saved and saved.get('transfer_id') == transfer_id:
            part_bytes = min(part_bytes, saved['part_bytes'])
        with path.open('rb') as stream:
            cursor = None
            while True:
                args = {'transfer_id': transfer_id, 'limit': 50}
                if cursor:
                    args['cursor'] = cursor
                status = self.checked(await self.call('transfer.status', args))
                require(
                    status.data['size'] == size and status.data['digest'] == hashed,
                    'transfer_source_mismatch',
                )
                if status.data['state'] == 'sealed':
                    break
                require(status.data['state'] == 'open', 'transfer_not_open')
                for start, end in status.data['missing']:
                    stream.seek(start)
                    while start < end:
                        piece = stream.read(min(part_bytes, end - start))
                        require(bool(piece), 'source_changed_during_upload')
                        self.checked(
                            await self.call(
                                'transfer.part_put',
                                {
                                    'transfer_id': transfer_id,
                                    'offset': start,
                                    'data': b64(piece),
                                    'digest': digest(piece),
                                },
                            )
                        )
                        start += len(piece)
                cursor = status.data.get('next_cursor')
                if cursor is None:
                    break
        result = self.checked(
            await self.call(
                'transfer.seal',
                {'transfer_id': transfer_id, 'final_size': size, 'final_digest': hashed},
            )
        )
        journal.unlink(missing_ok=True)
        return result

    async def download(self, resource, path, *, transfer_id=None, part_bytes=65536):
        require(type(part_bytes) is int and part_bytes > 0, 'invalid_part_bytes')
        ref = resource if isinstance(resource, ResourceRef) else ResourceRef(id=resource)
        path = Path(path)
        journal = self.state.file(
            'download-' + digest((wire(ref), str(path.resolve())))[7:39] + '.json'
        )
        part = path.with_name(path.name + '.msg-part')
        saved = loads(journal.read_bytes()) if journal.exists() else None
        if transfer_id is None and saved:
            transfer_id = saved['transfer_id']
        if transfer_id is None:
            limits = await self.transport.discover()
            result = self.checked(
                await self.call(
                    'transfer.open',
                    {
                        'direction': 'download',
                        'target': wire(ref),
                        'requested_part_bytes': part_bytes,
                        'max_request_bytes': limits.max_request_bytes,
                        'max_response_bytes': limits.max_response_bytes,
                        'max_path_bytes': limits.max_path_bytes,
                    },
                )
            )
            saved = {
                'transfer_id': result.data['transfer_id'],
                'size': result.data['size'],
                'digest': result.data['digest'],
                'part_bytes': result.data['part_bytes'],
                'offset': 0,
                'prefix_digest': digest(b''),
            }
            transfer_id = saved['transfer_id']
            durable_write(journal, canonical(saved), mode=0o600)
        require(saved is not None, 'download_journal_required')
        require(not path.exists() and not part.is_symlink(), 'download_target_exists')
        path.parent.mkdir(parents=True, exist_ok=True)
        offset = saved['offset']
        hasher = hashlib.sha256()
        if offset:
            require(part.is_file() and part.stat().st_size >= offset, 'download_partial_missing')
            with part.open('rb') as source:
                remaining = offset
                while remaining:
                    piece = source.read(min(65536, remaining))
                    require(bool(piece), 'download_partial_missing')
                    hasher.update(piece)
                    remaining -= len(piece)
            require(
                'sha256:' + hasher.hexdigest() == saved['prefix_digest'],
                'download_partial_modified',
            )
        mode = 'r+b' if part.exists() else 'w+b'
        with part.open(mode) as out:
            part.chmod(0o600)
            out.truncate(offset)
            out.seek(offset)
            while offset < saved['size']:
                amount = min(part_bytes, saved['part_bytes'], saved['size'] - offset)
                result = self.checked(
                    await self.call(
                        'transfer.part_get',
                        {'transfer_id': transfer_id, 'offset': offset, 'length': amount},
                    )
                )
                piece = unb64(result.data['data'])
                require(
                    len(piece) == amount
                    and digest(piece) == result.data['chunk']['content']['digest'],
                    'download_chunk_digest_mismatch',
                )
                out.write(piece)
                out.flush()
                os.fsync(out.fileno())
                hasher.update(piece)
                offset += len(piece)
                saved.update(offset=offset, prefix_digest='sha256:' + hasher.hexdigest())
                durable_write(journal, canonical(saved), mode=0o600)
        require('sha256:' + hasher.hexdigest() == saved['digest'], 'download_digest_mismatch')
        # Link avoids overwriting a target created concurrently.
        os.link(part, path)
        part.unlink()
        journal.unlink(missing_ok=True)
        return {
            'path': str(path),
            'size': saved['size'],
            'digest': saved['digest'],
            'transfer_id': transfer_id,
        }

    async def ack(self, ref):
        require(ref.revision is not None, 'ack_revision_required')
        value = self.checked(
            await self.call(
                'discovery.get',
                {'id': ref.id, 'revision': ref.revision, 'fields': ['id', 'revision', 'digest']},
            )
        )
        return await self.call(
            'discussion.ack', {'target': wire(ref), 'digest': value.data['digest']}
        )

    async def contract_registry(self):
        result = self.checked(await self.call('discovery.operations', {}, anonymous=True))
        return RemoteRegistry(self, result.data)


class RemoteRegistry:
    """Read-only contract cache; never loads code or trusts server principals."""

    def __init__(self, client, catalog):
        from types import SimpleNamespace

        self.client = client
        self._catalog = dict(catalog)
        self._specs = {}
        self._schemas = {}
        self.directory = client.state.cache_directory / catalog['digest'].replace(':', '-')
        from msg.paths import private_directory

        private_directory(self.directory)
        for item in catalog['operations']:
            key = (item['name'], item['version'])
            require(key not in self._specs, 'duplicate_operation')
            require(
                'network' in item['entries'] and not item['name'].startswith('root.'),
                'invalid_network_contract',
            )
            self._specs[key] = SimpleNamespace(**{
                **item,
                'entries': frozenset(item['entries']),
                'input_schema': decode(ResourceRef, item['input_schema']),
                'output_schema': decode(ResourceRef, item['output_schema']),
            })

    def catalog(self):
        return self._catalog

    def operations(self, entry='network'):
        return tuple(
            self._specs[key] for key in sorted(self._specs) if entry in self._specs[key].entries
        )

    def operation(self, name, version=1):
        require((name, version) in self._specs, 'unknown_operation')
        return self._specs[(name, version)]

    def schema(self, ref):
        require(ref.id in self._schemas, 'schema_not_cached')
        return self._schemas[ref.id]

    async def load_schemas(self, specs):
        async def load(spec):
            path = self.directory / (digest((spec.name, spec.version))[7:39] + '.json')
            if path.is_file():
                value = loads(path.read_bytes())
            else:
                identity = spec.name if spec.version == 1 else f'{spec.name}@{spec.version}'
                result = self.client.checked(
                    await self.client.call(
                        'discovery.schema', {'operation': identity}, anonymous=True
                    )
                )
                value = dict(result.data)
                durable_write(path, canonical(value), mode=0o600)
            require(
                value['operation']['name'] == spec.name
                and value['operation']['version'] == spec.version,
                'schema_contract_mismatch',
            )
            self._schemas[spec.input_schema.id] = value['input']
            self._schemas[spec.output_schema.id] = value['output']

        await asyncio.gather(*(load(s) for s in specs if s.input_schema.id not in self._schemas))
