"""Principal construction is a trusted server operation, never request data."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace

from msg.constants import ROOT_SUBJECT
from msg.core.codec import digest, parse_time, unb64
from msg.core.errors import Failure, require
from msg.core.models import Principal, SignatureProof, TokenProof
from msg.core.requests import payload_fields, signing_bytes
from msg.security.crypto import key_id, subject_id, verify
from msg.security.quarantine import RuntimeGeneration, active as quarantine_active
from msg.security.token_delivery import recovery_verifier

CUSTODIAL_SIGNED_WRITES = frozenset({
    'identity.oauth_approve',
    'content.post_create',
    'content.post_edit',
    'content.file_put',
    'content.attach',
    'file.create',
    'file.copy',
    'file.write',
    'file.patch',
    'discussion.reply',
    'discussion.quote',
    'discussion.repost',
    'identity.token_rotate',
    'identity.custodial_upgrade_start',
    'identity.custodial_upgrade_finish',
    'identity.custodial_rewrap_entry',
    'identity.custodial_rewrap_revision',
    'identity.custodial_rewrap_ack',
    'achievement.start',
    'achievement.answer',
    'achievement.finish',
    'achievement.pin',
    'achievement.unpin',
    'achievement.reorder',
})


class AuthenticationService:
    def __init__(self, registry, certificates, service, clock, primary_ceiling, temporary_ceiling):
        self.registry, self.certificates, self.service, self.clock = (
            registry,
            certificates,
            service,
            clock,
        )
        self.primary_ceiling, self.temporary_ceiling = primary_ceiling, temporary_ceiling
        self.runtime_generation = RuntimeGeneration()
        self.oauth_config = None

    async def authenticate(self, request, session, *, entry):
        self.runtime_generation.require_current(session)
        require(
            session.setting(
                'active_root_certificate', self.certificates.root_certificate.resource_id
            )
            == self.certificates.root_certificate.resource_id,
            'service_restart_required',
        )
        spec = self.registry.operation(request.operation, request.contract_version)
        if quarantine_active(session):
            require(spec.effect == 'read', 'writes_paused')
            raise Failure('recovery_quarantined')
        require(
            request.protocol_version == 1 and request.target_service == self.service,
            'wrong_service',
        )
        require(
            request.payload_digest == digest(payload_fields(request)), 'payload_digest_mismatch'
        )
        now = self.clock()
        if spec.effect != 'read' or request.proof is not None:
            require(request.expires_at is not None and now < request.expires_at, 'request_expired')
            require((request.expires_at - now).total_seconds() <= 300, 'request_expiry_too_long')
        proof = request.proof
        if request.operation == 'identity.register':
            require(isinstance(proof, SignatureProof), 'proof_required')
            public = unb64(request.arguments['public_key'], limit=32)
            require(len(public) == 32 and request.subject == subject_id(public), 'subject_mismatch')
            verify(public, signing_bytes(request), proof.signature, purpose='request')
            return Principal(
                actor=request.subject,
                subject=request.subject,
                credential_id=key_id(public),
                method='signature',
                certificates=(),
                ceiling=self.primary_ceiling(),
            )
        if request.operation in {'identity.temporary', 'identity.custodial_create'}:
            require(proof is None and request.subject is None, 'invalid_bootstrap')
            nonce = unb64(request.arguments['nonce'], limit=64)
            require(len(nonce) >= 24, 'invalid_bootstrap_nonce')
            # The high-entropy claim defines the initial temporary subject; ordinary anonymous
            # reads cannot create it. The token itself is derived only by the server.
            prefix = 'u_cust_' if request.operation == 'identity.custodial_create' else 'u_tmp_'
            subject = prefix + hashlib.sha256(nonce).hexdigest()[:32]
            return Principal(
                actor=subject,
                subject=subject,
                credential_id='t_' + subject[2:],
                method='token',
                certificates=(),
                ceiling=self.temporary_ceiling(),
            )
        if request.operation == 'identity.token_recover':
            require(proof is None and request.subject is not None, 'invalid_recovery_proof')
            old = await session.credential(request.arguments['credential_id'])
            from msg.security.oauth import require_binding

            await require_binding(
                session, old, now, self.oauth_config, custodial_ceiling=self.temporary_ceiling()
            )
            require(
                old.kind == 'token' and old.subject_id == request.subject, 'recovery_unavailable'
            )
            actor = await session.subject(old.subject_id)
            require(not actor.local_only and actor.resource_id != ROOT_SUBJECT, 'local_only')
            verifier = recovery_verifier(request.arguments['recovery_secret'])
            row = session.one(
                """SELECT recovery_verifier,recovery_expires_at,consumed_at,request_id
                FROM token_deliveries WHERE credential_id=? AND subject=?""",
                (old.id, actor.resource_id),
            )
            require(
                row is not None
                and hmac.compare_digest(row[0], verifier)
                and parse_time(row[1]) > now
                and row[3] == request.arguments['original_request_id'],
                'recovery_unavailable',
            )
            if row[2] is not None:
                previous = await session.request_result(
                    actor.resource_id, request.request_id, request.payload_digest
                )
                require(
                    previous is not None and previous.data.get('previous_credential') == old.id,
                    'recovery_unavailable',
                )
            else:
                require(old.revoked_at is None and old.expires_at > now, 'recovery_unavailable')
            operation = 'identity.token_recover@1'
            ceiling = tuple(
                replace(g, operations=frozenset({operation}))
                for g in self.primary_ceiling()
                if g.capability == 'identity.basic' and operation in g.operations
            )
            require(bool(ceiling), 'recovery_unavailable')
            return Principal(
                actor=actor.resource_id,
                subject=actor.resource_id,
                credential_id=old.id,
                method='recovery',
                certificates=(),
                ceiling=ceiling,
            )
        if proof is None:
            require(request.subject is None and spec.effect == 'read', 'authentication_required')
            return Principal(
                actor=None,
                subject=None,
                credential_id=None,
                method='anonymous',
                certificates=(),
                ceiling=(),
            )
        credential_id = (
            proof.signature.key_id if isinstance(proof, SignatureProof) else proof.credential_id
        )
        credential = await session.credential(credential_id)
        actor = await session.subject(credential.subject_id)
        require(
            credential.not_before <= now
            and (credential.expires_at is None or now < credential.expires_at),
            'credential_expired',
        )
        if isinstance(proof, SignatureProof):
            require(credential.kind == 'signing_key', 'wrong_credential_kind')
            verify(credential.verifier, signing_bytes(request), proof.signature, purpose='request')
            method = 'signature'
            ids = proof.certificates
        elif isinstance(proof, TokenProof):
            require(credential.kind == 'token', 'wrong_credential_kind')
            require(
                hmac.compare_digest(credential.verifier, hashlib.sha256(proof.token).digest()),
                'invalid_token',
            )
            method = 'token'
            ids = ()
            from msg.security.oauth import require_binding

            await require_binding(
                session,
                credential,
                now,
                self.oauth_config,
                custodial_ceiling=self.temporary_ceiling(),
            )
        else:
            raise Failure('invalid_proof')
        if credential.revoked_at is not None:
            # A token invalidated by its own successful rotation may only retrieve
            # that exact committed rotation result. It cannot authorize new work.
            previous = None
            if method == 'token' and request.operation in {
                'identity.token_rotate',
                'identity.custodial_upgrade_finish',
            }:
                previous = await session.request_result(
                    actor.resource_id, request.request_id, request.payload_digest
                )
            require(
                previous is not None and previous.data.get('previous_credential') == credential.id,
                'credential_revoked',
            )
            successor = await session.credential(previous.data['credential_id'])
            require(
                successor.subject_id == actor.resource_id
                and successor.revoked_at is None
                and (successor.expires_at is None or successor.expires_at > now),
                'credential_revoked',
            )
        subject = await session.subject(request.subject or actor.resource_id)
        require(
            not (
                actor.local_only
                or subject.local_only
                or actor.resource_id == ROOT_SUBJECT
                or subject.resource_id == ROOT_SUBJECT
            )
            or entry == 'local_admin',
            'local_only',
        )
        require(entry in spec.entries, 'entry_not_allowed')
        operation = f'{spec.name}@{spec.version}'
        require(any(operation in g.operations for g in credential.ceiling), 'credential_ceiling')
        if actor.kind == 'custodial' and method == 'token' and spec.effect != 'read':
            download_only = (
                spec.name == 'transfer.open' and request.arguments.get('direction') == 'download'
            )
            require(
                spec.name in CUSTODIAL_SIGNED_WRITES or download_only,
                'custodial_operation_not_supported',
            )
        # Achievement handlers sign explicit confirmations with the current
        # vault key; ordinary token subjects still need a signature.
        custodial_confirmation = (
            actor.kind == 'custodial'
            and method == 'token'
            and spec.name
            in {'achievement.finish', 'achievement.pin', 'achievement.unpin', 'achievement.reorder'}
        )
        require(
            not spec.require_signature or method == 'signature' or custodial_confirmation,
            'signature_required',
        )
        valid = []
        for cid in ids:
            cert = await self.certificates.validate(cid, session)
            require(
                cert.subject_id == actor.resource_id and cert.key_id == credential.id,
                'certificate_subject_mismatch',
            )
            valid.append(cert)
        if actor.resource_id != subject.resource_id:
            require(
                any(
                    c.kind == 'delegation'
                    and any(
                        (session.setting('delegation:' + s.id) or {}).get('grantor')
                        == subject.resource_id
                        for s in c.authority_sources
                    )
                    for c in valid
                ),
                'delegation_required',
            )
        return Principal(
            actor=actor.resource_id,
            subject=subject.resource_id,
            credential_id=credential.id,
            method=method,
            certificates=tuple(c.resource_id for c in valid),
            ceiling=credential.ceiling,
        )
