"""One-time credential delivery, distinct from service composition.

The handler records the verifier in its existing issuance transaction. After that
transaction commits, release claims the delivery in a separate write transaction;
only a committed claim can produce a response-only token. Lost responses must use
the pre-bound recovery operation, never replay a persistent secret. This module
owns no signer, vault, root capability, HTTP adapter or plugin registry.

Clock and window providers preserve the configured current values without giving
the domain access to the whole settings/Application object. These are trusted
in-process dependencies, not a Python sandbox or an additional authorization path.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import hmac

from msg.core.codec import b64, canonical, unb64, wire
from msg.core.contracts import MetadataStore
from msg.core.errors import require
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION


def recovery_verifier(value):
    secret=unb64(value,limit=64)
    require(len(secret)>=32,'invalid_recovery_secret')
    return hashlib.sha256(b'token-recovery-v1\0'+secret).hexdigest()

class TokenDelivery:
    def __init__(self, *, metadata: MetadataStore, token_secret: bytes,
                 clock: Callable[[], datetime], recovery_window: Callable[[], int]):
        require(len(token_secret)==32,'invalid_service_key')
        self.metadata=metadata
        self._token_secret=token_secret
        self.clock=clock
        self.recovery_window=recovery_window

    def issued_token(self,request,subject):
        # Nonces are high-entropy claims, not random tokens persisted in results.
        return hmac.digest(self._token_secret,b'issued-token-v1\0'+canonical({
            'subject':subject,'request_id':request.request_id,'nonce':request.arguments['nonce']}),'sha256')

    recovery_verifier=staticmethod(recovery_verifier)

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
                    now+timedelta(seconds=self.recovery_window()))
        require(expires>now,'recovery_unavailable')
        tx.execute('''INSERT INTO token_deliveries
            (credential_id,subject,request_id,request_digest,recovery_verifier,recovery_expires_at,
             claimed_at,consumed_at) VALUES (?,?,?,?,?,?,NULL,NULL)''',
            (credential.id,credential.subject_id,request.request_id,request.payload_digest,
             self.recovery_verifier(request.arguments[field]),wire(expires)),write=True)

    async def release(self,request,result):
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
