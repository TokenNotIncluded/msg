"""Explicit composition root for network services. Contains no root signer."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import hashlib
import hmac
import importlib

from msg.constants import ROOT_SPACE, ROOT_SUBJECT
from msg.core.codec import loads, decode, unb64, b64, canonical
from msg.core.cursors import CursorCodec
from msg.core.errors import Failure, require
from msg.core.executor import OperationExecutor
from msg.core.models import Certificate, Scope
from msg.core.registry import Registry
from msg.security.authentication import AuthenticationService
from msg.security.authorization import AuthorizationService
from msg.security.capabilities import install_capabilities, primary_ceiling, base_grants, temporary_ceiling
from msg.security.certificates import CertificateValidator
from msg.security.crypto import Ed25519Signer
from msg.storage.git import GitContentStore
from msg.storage.postgres import PostgresMetadataStore
from msg.storage.valkey_bus import ValkeyOutboxSignal


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
        implemented=('identity','content','discussion','communication','discovery','achievements')
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
        self.cursors=CursorCodec(hmac.digest(self._token_secret,b'cursor-key-v1','sha256'))
        self.certificates=CertificateValidator(self.registry,root_certificate,root_public,self.settings.service_url,self.clock)
        async with self.metadata.transaction(write=False) as tx:
            await self.certificates.validate(root_certificate.resource_id,tx)
            root=await tx.subject(ROOT_SUBJECT)
            require(root.local_only,'root_policy_corrupt')
        self.authenticator=AuthenticationService(self.registry,self.certificates,self.settings.service_url,self.clock,
            self.primary_ceiling,self.temporary_ceiling)
        self.authorizer=AuthorizationService(self.registry,self.certificates)
        self.executor=OperationExecutor(self.registry,self.metadata,self.contents,self.authenticator,self.authorizer,
                                       self.clock,self.receipt_signer)
        self.executor.application=self
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

    async def _secrets_for_caller(self,request,result):
        if result.status!='ok' or request.operation not in {'identity.temporary','identity.token_rotate','identity.token_create'}:
            return result
        token=self.issued_token(request,result.subject)
        async with self.metadata.transaction(write=False) as tx:
            credential=await tx.credential(result.data['credential_id'])
            require(credential.revoked_at is None and credential.expires_at>self.clock(),'credential_expired')
            require(hmac.compare_digest(credential.verifier,hashlib.sha256(token).digest()),'invalid_token_result')
        # This one-time credential delivery is deliberately excluded from durable
        # metadata and the signed business receipt. The verifier is in storage.
        return replace(result,data=dict(result.data,token=b64(token)))

    async def close(self):
        if self.metadata is not None:
            await self.metadata.close()
