"""Explicit composition root for network services. Contains no root signer."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime

from msg.constants import ROOT_SPACE, ROOT_SUBJECT
from msg.core.codec import decode, loads, unb64
from msg.core.cursors import CursorCodec
from msg.core.errors import require
from msg.core.executor import OperationExecutor
from msg.core.models import Certificate, Scope
from msg.core.registry import Registry
from msg.plugins import install_registry
from msg.security.authentication import AuthenticationService
from msg.security.authorization import AuthorizationService
from msg.security.capabilities import base_grants, primary_ceiling, temporary_ceiling
from msg.security.certificates import CertificateValidator
from msg.security.crypto import Ed25519Signer
from msg.security.quarantine import RuntimeGeneration, active as quarantine_active
from msg.security.token_delivery import TokenDelivery, recovery_verifier
from msg.storage.git import GitContentStore


class Application:
    def __init__(self, settings, *, clock=None, selftest_run_id=None):
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        # Only the local diagnostic constructs an isolated test namespace.
        require(
            selftest_run_id is None
            or (
                isinstance(selftest_run_id, str)
                and len(selftest_run_id) == 32
                and all(c in '0123456789abcdef' for c in selftest_run_id)
            ),
            'invalid_selftest_run_id',
        )
        self.selftest_run_id = selftest_run_id
        self.namespace_root = (
            ROOT_SPACE if selftest_run_id is None else 't_selftest_' + selftest_run_id
        )
        self.registry = Registry()
        self.metadata = None
        self.contents = None
        self.executor = None
        self.token_delivery = None
        self._loaded = False
        self.runtime_generation = None
        install_registry(self, settings.server.plugins)

    def primary_ceiling(self):
        return primary_ceiling(self.registry, self.default_scope())

    def base_grants(self):
        return base_grants(self.registry, self.default_scope())

    def default_scope(self):
        return Scope(resource_id=self.namespace_root, descendants=True)

    @property
    def base_capability_names(self):
        return frozenset(g.capability for g in self.base_grants())

    def temporary_ceiling(self):
        return temporary_ceiling(self.registry, self.default_scope())

    async def open_storage(self):
        if self.metadata is None:
            from msg.storage.postgres import PostgresMetadataStore
            from msg.storage.valkey_bus import ValkeyOutboxSignal

            signal = (
                ValkeyOutboxSignal(self.settings.server.valkey_url)
                if self.settings.server.valkey_url
                else None
            )
            self.metadata = PostgresMetadataStore(self.settings.server.postgres_dsn, signal=signal)
        if self.contents is None:
            self.contents = GitContentStore(
                self.settings.server.content_dir,
                binary_dir=self.settings.server.blob_dir,
                staging_dir=self.settings.server.staging_dir,
                group_read=self.settings.hosting_content_group_read,
            )

    async def load(self):
        require(self.settings.trust_file.is_file(), 'root_not_initialized')
        # Pin before reading runtime files, then recheck with validated authority.
        await self.open_storage()
        async with self.metadata.transaction(write=False) as tx:
            if self.runtime_generation is None:
                self.runtime_generation = RuntimeGeneration.capture(tx)
            else:
                self.runtime_generation.require_current(tx)
        trust = loads(self.settings.trust_file.read_bytes())
        require(
            set(trust) == {'version', 'public_key', 'certificate'} and trust['version'] == 1,
            'invalid_trust_anchor',
        )
        root_certificate = decode(Certificate, trust['certificate'])
        root_public = unb64(trust['public_key'], limit=32)
        keys = self.settings.service_keys
        required = ('online.key', 'receipt.key', 'tokens.key')
        require(all((keys / name).is_file() for name in required), 'service_keys_missing')
        self.online_signer = Ed25519Signer.from_bytes((keys / 'online.key').read_bytes())
        self.receipt_signer = Ed25519Signer.from_bytes((keys / 'receipt.key').read_bytes())
        self._token_secret = (keys / 'tokens.key').read_bytes()
        require(len(self._token_secret) == 32, 'invalid_service_key')
        self._vault_key = hmac.digest(self._token_secret, b'custodial-vault-aesgcm-v1', 'sha256')
        self.cursors = CursorCodec(hmac.digest(self._token_secret, b'cursor-key-v1', 'sha256'))
        self.token_delivery = TokenDelivery(
            metadata=self.metadata,
            token_secret=self._token_secret,
            clock=lambda: self.clock(),
            recovery_window=lambda: self.settings.credential_delivery_recovery_window,
        )
        self.certificates = CertificateValidator(
            self.registry, root_certificate, root_public, self.settings.service_url, self.clock
        )
        async with self.metadata.transaction(write=False) as tx:
            self.runtime_generation.require_current(tx)
            self.token_delivery.runtime_generation = self.runtime_generation
            quarantined = quarantine_active(tx)
            await self.certificates.validate(root_certificate.resource_id, tx)
            root = await tx.subject(ROOT_SUBJECT)
            require(root.local_only, 'root_policy_corrupt')
        # Only a verified installation may import immutable release-owned rules.
        # This is idempotent per source digest/version and rejects source deletion.
        from msg.bootstrap import sync_system_sources

        marker = self.settings.config_dir / 'recovery-drill.json'
        quarantined = quarantined or marker.exists() or marker.is_symlink()
        if not quarantined:
            async with self.metadata.transaction(write=True) as tx:
                # Recheck under the writer lock; loading a restore must not
                # mutate release resources before recovery has been accepted.
                self.runtime_generation.require_current(tx)
                if not quarantine_active(tx):
                    await sync_system_sources(
                        tx,
                        self.contents,
                        self.clock(),
                        namespace_root=self.namespace_root,
                        registry=self.registry,
                    )
                else:
                    quarantined = True
        self.authenticator = AuthenticationService(
            self.registry,
            self.certificates,
            self.settings.service_url,
            self.clock,
            self.primary_ceiling,
            self.temporary_ceiling,
        )
        self.authorizer = AuthorizationService(self.registry, self.certificates)
        self.authenticator.runtime_generation = self.runtime_generation
        self.authorizer.runtime_generation = self.runtime_generation
        self.executor = self.new_executor(self.authenticator)
        self.executor.recovery_drill_marker = marker
        self.executor.recovery_quarantined = quarantined
        self._loaded = True
        return self

    def new_executor(self, authenticator=None):
        """Compose identical limits/ports for HTTP and the restricted SSH entry."""
        executor = OperationExecutor(
            self.registry,
            self.metadata,
            self.contents,
            self.authenticator if authenticator is None else authenticator,
            self.authorizer,
            self.clock,
            self.receipt_signer,
            max_request_bytes=self.settings.server.limits.max_request_bytes,
            result_projection=self._result_projection,
            event_notifications=self._event_notifications,
        )
        executor.runtime_generation = self.runtime_generation
        executor.response_hook = self.token_delivery.release
        executor.recovery_drill_marker = self.settings.config_dir / 'recovery-drill.json'
        if self.executor is not None:
            executor.recovery_quarantined = self.executor.recovery_quarantined
        return executor

    async def _result_projection(self, context, request, session, resource, *, fields):
        from msg.plugins.discovery import read_projection

        return await read_projection(
            self, context, request, session, resource.id, revision=resource.revision, fields=fields
        )

    async def _event_notifications(self, session, event):
        if 'communication' not in self.settings.server.plugins:
            return
        from msg.plugins.communication import enqueue_domain_webhooks

        await enqueue_domain_webhooks(self, session, event)
        from msg.plugins.watches import enqueue

        await enqueue(self, session, event)

    async def online_issuer(self, tx):
        issuer = tx.setting('online_ca_certificate')
        require(issuer is not None, 'issuer_not_ready')
        cert = await self.certificates.validate(issuer, tx)
        require(cert.kind == 'ca' and cert.key_id == self.online_signer.key_id, 'issuer_not_ready')
        return cert

    def issued_token(self, request, subject):
        return self.token_delivery.issued_token(request, subject)

    recovery_verifier = staticmethod(recovery_verifier)

    def record_token_delivery(self, tx, request, credential, now, *, recovery_deadline=None):
        return self.token_delivery.record_token_delivery(
            tx, request, credential, now, recovery_deadline=recovery_deadline
        )

    async def _secrets_for_caller(self, request, result):
        return await self.token_delivery.release(request, result)

    async def close(self):
        if self.metadata is not None:
            await self.metadata.close()
