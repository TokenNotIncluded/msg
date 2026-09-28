"""Explicit composition root for network services. Contains no root signer."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import hashlib
import hmac
import importlib

from msg.constants import ROOT_SPACE, ROOT_SUBJECT
from msg.core.codec import loads, decode, unb64, b64, canonical, wire
from msg.core.cursors import CursorCodec
from msg.core.errors import Failure, require
from msg.core.executor import OperationExecutor
from msg.core.models import Certificate, Scope
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION
from msg.core.registry import Registry
from msg.security.authentication import AuthenticationService
from msg.security.authorization import AuthorizationService
from msg.security.capabilities import install_capabilities, primary_ceiling, base_grants, temporary_ceiling
from msg.security.certificates import CertificateValidator
from msg.security.crypto import Ed25519Signer
from msg.storage.git import GitContentStore


class Application:
    def __init__(self,settings, *, clock=None, selftest_run_id=None):
        self.settings=settings
        self.clock=clock or (lambda: datetime.now(UTC))
        # Only the local diagnostic constructs an isolated test namespace.
        require(selftest_run_id is None or (isinstance(selftest_run_id,str) and
                len(selftest_run_id)==32 and all(c in '0123456789abcdef' for c in selftest_run_id)),
                'invalid_selftest_run_id')
        self.selftest_run_id=selftest_run_id
        self.namespace_root=ROOT_SPACE if selftest_run_id is None else 't_selftest_'+selftest_run_id
        self.registry=Registry()
        self.metadata=None
        self.contents=None
        self.executor=None
        self._loaded=False
        # Plugins are installed code, never resources, posts, or configuration expressions.
        implemented=('identity','content','discussion','communication','discovery','achievements','recovery','sharing','money','offers','store','bounty','orders','delivery')
        configured=set(settings.server.plugins)
        require(configured<=set(implemented)|{'transfer','extensions','system','batch'},'unknown_plugin')
        for plugin in implemented:
            if plugin in configured:
                importlib.import_module('msg.plugins.'+plugin).install(self)
        for plugin in ('transfer','extensions','system','batch'):
            if plugin in configured:
                importlib.import_module('msg.plugins.'+plugin).install(self)
        install_capabilities(self.registry)
        self.registry.freeze()

    def primary_ceiling(self):
        return primary_ceiling(self.registry,self.default_scope())

    def base_grants(self):
        return base_grants(self.registry,self.default_scope())

    def default_scope(self):
        return Scope(resource_id=self.namespace_root,descendants=True)

    @property
    def base_capability_names(self):
        return frozenset(g.capability for g in self.base_grants())

    def temporary_ceiling(self):
        return temporary_ceiling(self.registry,self.default_scope())

    async def open_storage(self):
        if self.metadata is None:
            from msg.storage.postgres import PostgresMetadataStore
            from msg.storage.valkey_bus import ValkeyOutboxSignal
            signal=(ValkeyOutboxSignal(self.settings.server.valkey_url)
                    if self.settings.server.valkey_url else None)
            self.metadata=PostgresMetadataStore(self.settings.server.postgres_dsn,signal=signal)
        if self.contents is None:
            self.contents=GitContentStore(self.settings.server.content_dir,
                                          binary_dir=self.settings.server.blob_dir,
                                          staging_dir=self.settings.server.staging_dir)

    async def load(self):
        require(self.settings.trust_file.is_file(),'root_not_initialized')
        trust=loads(self.settings.trust_file.read_bytes())
        require(set(trust)=={'version','public_key','certificate'} and trust['version']==1,'invalid_trust_anchor')
        root_certificate=decode(Certificate,trust['certificate'])
        root_public=unb64(trust['public_key'],limit=32)
        await self.open_storage()
        keys=self.settings.service_keys
        required=('online.key','receipt.key','tokens.key')
        require(all((keys/name).is_file() for name in required),'service_keys_missing')
        self.online_signer=Ed25519Signer.from_bytes((keys/'online.key').read_bytes())
        self.receipt_signer=Ed25519Signer.from_bytes((keys/'receipt.key').read_bytes())
        self._token_secret=(keys/'tokens.key').read_bytes()
        require(len(self._token_secret)==32,'invalid_service_key')
        self._vault_key=hmac.digest(self._token_secret,b'custodial-vault-aesgcm-v1','sha256')
        self.cursors=CursorCodec(hmac.digest(self._token_secret,b'cursor-key-v1','sha256'))
        self.certificates=CertificateValidator(self.registry,root_certificate,root_public,self.settings.service_url,self.clock)
        async with self.metadata.transaction(write=False) as tx:
            await self.certificates.validate(root_certificate.resource_id,tx)
            root=await tx.subject(ROOT_SUBJECT)
            require(root.local_only,'root_policy_corrupt')
        # Only a verified installation may import immutable release-owned rules.
        # This is idempotent per source digest/version and rejects source deletion.
        from msg.bootstrap import sync_system_sources
        async with self.metadata.transaction(write=True) as tx:
            await sync_system_sources(tx,self.contents,self.clock(),namespace_root=self.namespace_root)
        self.authenticator=AuthenticationService(self.registry,self.certificates,self.settings.service_url,self.clock,
            self.primary_ceiling,self.temporary_ceiling)
        self.authorizer=AuthorizationService(self.registry,self.certificates)
        self.executor=OperationExecutor(self.registry,self.metadata,self.contents,self.authenticator,self.authorizer,
                                       self.clock,self.receipt_signer)
        self.executor.application=self
        self.executor.recovery_drill_marker=self.settings.config_dir/'recovery-drill.json'
        self.executor.response_hook=self._secrets_for_caller
        self._loaded=True
        return self

    async def online_issuer(self,tx):
        issuer=tx.setting('online_ca_certificate')
        require(issuer is not None,'issuer_not_ready')
        cert=await self.certificates.validate(issuer,tx)
        require(cert.kind=='ca' and cert.key_id==self.online_signer.key_id,'issuer_not_ready')
        return cert

    def issued_token(self,request,subject):
        # Nonces are high-entropy claims, not random tokens persisted in results.
        return hmac.digest(self._token_secret,b'issued-token-v1\0'+canonical({
            'subject':subject,'request_id':request.request_id,'nonce':request.arguments['nonce']}),'sha256')

    @staticmethod
    def recovery_verifier(value):
        secret=unb64(value,limit=64)
        require(len(secret)>=32,'invalid_recovery_secret')
        return hashlib.sha256(b'token-recovery-v1\0'+secret).hexdigest()

    def record_token_delivery(self,tx,request,credential,now,*,recovery_deadline=None):
        """Bind recovery before commit; neither token nor recovery secret is stored."""
        require(request.operation in SECRET_DELIVERY_MIN_VERSION and
                request.contract_version>=SECRET_DELIVERY_MIN_VERSION[request.operation],
                'credential_delivery_upgrade_required')
        field='new_recovery_secret' if request.operation=='identity.token_recover' else 'recovery_secret'
        secret=unb64(request.arguments[field],limit=64)
        require(secret!=unb64(request.arguments['nonce'],limit=64),'recovery_secret_not_independent')
        if request.operation=='identity.token_recover':
            require(secret!=unb64(request.arguments['recovery_secret'],limit=64),
                    'recovery_secret_not_independent')
        expires=min(credential.expires_at,
                    recovery_deadline if recovery_deadline is not None else
                    now+timedelta(seconds=self.settings.credential_delivery_recovery_window))
        require(expires>now,'recovery_unavailable')
        tx.execute('''INSERT INTO token_deliveries
            (credential_id,subject,request_id,request_digest,recovery_verifier,recovery_expires_at,
             claimed_at,consumed_at) VALUES (?,?,?,?,?,?,NULL,NULL)''',
            (credential.id,credential.subject_id,request.request_id,request.payload_digest,
             self.recovery_verifier(request.arguments[field]),wire(expires)),write=True)

    async def _secrets_for_caller(self,request,result):
        if result.status!='ok' or request.operation not in SECRET_DELIVERY_MIN_VERSION:
            return result
        require(request.contract_version>=SECRET_DELIVERY_MIN_VERSION[request.operation],
                'credential_delivery_upgrade_required')
        token=self.issued_token(request,result.subject)
        async with self.metadata.transaction(write=True) as tx:
            credential=await tx.credential(result.data['credential_id'])
            require(credential.revoked_at is None and credential.expires_at>self.clock(),'credential_expired')
            require(hmac.compare_digest(credential.verifier,hashlib.sha256(token).digest()),'invalid_token_result')
            row=tx.one('''SELECT subject,request_id,request_digest,claimed_at,consumed_at
                FROM token_deliveries WHERE credential_id=?''',(credential.id,))
            require(row is not None and row[0]==result.subject and row[1]==request.request_id
                    and row[2]==request.payload_digest and row[4] is None,'token_delivery_unavailable',
                    details={'committed_result':wire(result,compact=True)})
            require(row[3] is None,'token_delivery_unavailable',
                    details={'committed_result':wire(result,compact=True)})
            updated=tx.execute('''UPDATE token_deliveries SET claimed_at=? WHERE credential_id=?
                AND claimed_at IS NULL AND consumed_at IS NULL''',
                (wire(self.clock()),credential.id),write=True)
            require(updated.rowcount==1,'token_delivery_unavailable',
                    details={'committed_result':wire(result,compact=True)})
        # The claim commits before the adapter sees the token. A dropped response
        # must use a separate, pre-bound recovery secret to rotate the credential.
        return replace(result,data=dict(result.data,token=b64(token)))

    async def close(self):
        if self.metadata is not None:
            await self.metadata.close()
