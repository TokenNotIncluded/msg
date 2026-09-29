"""Current-state certificate chain validation and distinct issuance ceilings."""

from __future__ import annotations

from dataclasses import replace

from msg.constants import ONLINE_CA, ROOT_SUBJECT
from msg.core.codec import canonical, digest, wire
from msg.core.errors import require
from msg.security.crypto import key_id, verify
from msg.security.policy import constraints_subset, grant_covers, scope_subset

# Keep this list explicit. Adding a new ordinary capability must not silently
# enlarge the permanent online issuer's signing authority.
ONLINE_ISSUABLE_CAPABILITIES = frozenset({
    'identity.basic',
    'resource.basic',
    'discussion.basic',
    'communication.basic',
    'discovery.basic',
    'transfer.basic',
    'group.basic',
    'cert.request',
    'git.basic',
    'hosting.basic',
    'keystore.basic',
    'batch.basic',
    'sharing.basic',
    'money.basic',
    'store.basic',
    'bounty.basic',
    'orders.basic',
    'delivery.basic',
})


def certificate_body(certificate):
    body = wire(certificate)
    body.pop('signature')
    return body


def csr_body(request):
    body = wire(request)
    for key in ('resource_id', 'request_digest', 'possession_proof'):
        body.pop(key, None)
    return body


def sign_certificate(certificate, signer):
    return replace(
        certificate,
        signature=signer.sign(canonical(certificate_body(certificate)), purpose='certificate'),
    )


def verify_csr(request):
    require(request.request_digest == digest(csr_body(request)), 'csr_digest_mismatch')
    verify(
        request.public_key, canonical(csr_body(request)), request.possession_proof, purpose='csr'
    )


class CertificateValidator:
    def __init__(self, registry, root_certificate, root_public_key, service, clock):
        self.registry = registry
        self.root_certificate = root_certificate
        self.root_public_key = root_public_key
        self.service = service
        self.clock = clock

    async def validate_grant(self, grant, session):
        spec = self.registry.capability(grant.capability, grant.version)
        resource = await session.resource(grant.scope.resource_id)
        require(resource.type in spec.scope_types, 'invalid_scope_type')
        require(
            bool(grant.operations) and grant.operations <= spec.operations,
            'invalid_grant_operations',
        )
        if spec.constraints_schema is not None:
            self.registry.validate(spec.constraints_schema, grant.constraints)
        else:
            require(not grant.constraints, 'unknown_grant_constraint')
        return spec

    async def allowed_issuance(self, grant, policy, session):
        await self.validate_grant(grant, session)
        for allowed in policy.issue_grants:
            if (
                grant.capability == allowed.capability
                and grant.version == allowed.version
                and grant.operations <= allowed.operations
                and await scope_subset(grant.scope, allowed.scope, session)
                and constraints_subset(grant.constraints, allowed.constraints)
            ):
                return True
        return False

    async def validate(self, id, session, *, seen=None, certificate=None, issuance_resource=None):
        seen = set() if seen is None else set(seen)
        require(id not in seen and len(seen) < 32, 'certificate_cycle')
        seen.add(id)
        cert = certificate or await session.certificate(id)
        now = self.clock()
        require(cert.target_service == self.service, 'wrong_service')
        require(cert.not_before <= now < cert.expires_at, 'certificate_expired')
        if certificate is None:
            require(not await session.certificate_revoked(id), 'certificate_revoked')
        if id == self.root_certificate.resource_id:
            require(canonical(cert) == canonical(self.root_certificate), 'trust_anchor_mismatch')
            require(
                cert.subject_id == ROOT_SUBJECT
                and cert.issuer_id == ROOT_SUBJECT
                and cert.parent_certificate_id is None
                and cert.kind == 'ca',
                'invalid_root_certificate',
            )
            verify(
                self.root_public_key,
                canonical(certificate_body(cert)),
                cert.signature,
                purpose='certificate',
            )
            return cert
        require(
            cert.subject_id != ROOT_SUBJECT and cert.parent_certificate_id is not None, 'local_only'
        )
        parent = await self.validate(cert.parent_certificate_id, session, seen=seen)
        require(parent.kind == 'ca' and parent.issuance is not None, 'issuer_not_ca')
        require(cert.issuer_id == parent.subject_id, 'issuer_mismatch')
        parent_key = await session.credential(parent.key_id)
        require(
            parent_key.subject_id == parent.subject_id and parent_key.revoked_at is None,
            'issuer_key_revoked',
        )
        verify(
            parent_key.verifier,
            canonical(certificate_body(cert)),
            cert.signature,
            purpose='certificate',
        )
        subject_key = await session.credential(cert.key_id)
        require(subject_key.subject_id == cert.subject_id, 'certificate_key_mismatch')
        require(
            subject_key.revoked_at is None
            and subject_key.not_before <= now
            and (subject_key.expires_at is None or now < subject_key.expires_at),
            'certificate_key_revoked',
        )
        require(
            parent.not_before <= cert.not_before and cert.expires_at <= parent.expires_at,
            'certificate_validity_escalation',
        )
        require(
            (cert.expires_at - cert.not_before).total_seconds()
            <= parent.issuance.max_cert_ttl_seconds,
            'certificate_ttl_escalation',
        )
        require(
            cert.delegation_depth <= parent.issuance.max_delegation_depth,
            'delegation_depth_exceeded',
        )
        # Signing rights are explicit use grants, not inferred from issue_grants.
        issue_resource = issuance_resource or session.setting(
            'certificate_request:' + cert.resource_id, cert.resource_id
        )
        require(
            any([
                await grant_covers(g, 'cert.issue', 'cert.publish@1', issue_resource, session)
                for g in parent.grants
            ]),
            'issuer_cannot_issue',
        )
        for grant in cert.grants:
            require(
                await self.allowed_issuance(grant, parent.issuance, session),
                'issuance_scope_exceeded',
            )
        if cert.subject_id == ONLINE_CA:
            require(
                parent.resource_id == self.root_certificate.resource_id
                and cert.kind == 'ca'
                and cert.issuance is not None
                and cert.issuance.max_child_ca_depth == 0
                and len(cert.grants) == 1
                and cert.grants[0].capability == 'cert.issue'
                and 'cert.publish@1' in cert.grants[0].operations,
                'online_ca_policy_exceeded',
            )
            require(
                all(
                    grant.capability in ONLINE_ISSUABLE_CAPABILITIES
                    and not self.registry.capability(grant.capability, grant.version).ca_only
                    for grant in cert.issuance.issue_grants
                ),
                'online_ca_policy_exceeded',
            )
        if parent.subject_id == ONLINE_CA:
            require(
                cert.kind in {'identity', 'delegation'}
                and (cert.kind != 'delegation' or bool(cert.authority_sources))
                and all(grant.capability in ONLINE_ISSUABLE_CAPABILITIES for grant in cert.grants),
                'online_ca_policy_exceeded',
            )
        if cert.kind == 'ca':
            require(
                cert.issuance is not None and parent.issuance.max_child_ca_depth > 0,
                'ca_depth_exceeded',
            )
            require(
                any([
                    await grant_covers(
                        g, 'cert.ca.issue', 'cert.publish@1', issue_resource, session
                    )
                    for g in parent.grants
                ]),
                'issuer_cannot_issue_ca',
            )
            require(
                cert.issuance.max_child_ca_depth < parent.issuance.max_child_ca_depth,
                'ca_depth_exceeded',
            )
            # The signed policy on an old trust anchor may allow more levels.
            # Count the validated parent chain so an L4 CA remains impossible.
            level = 1
            ancestor = parent
            while ancestor.resource_id != self.root_certificate.resource_id:
                level += 1
                ancestor = await session.certificate(ancestor.parent_certificate_id)
            require(
                level <= 3 and 0 <= cert.issuance.max_child_ca_depth <= 3 - level,
                'ca_depth_exceeded',
            )
            require(
                cert.issuance.max_cert_ttl_seconds <= parent.issuance.max_cert_ttl_seconds
                and cert.issuance.max_delegation_depth <= parent.issuance.max_delegation_depth,
                'issuance_policy_escalation',
            )
            for grant in cert.issuance.issue_grants:
                require(
                    await self.allowed_issuance(grant, parent.issuance, session),
                    'issuance_scope_exceeded',
                )
        else:
            require(cert.issuance is None, 'unexpected_issuance_policy')
        for source in cert.authority_sources:
            await self.validate_authority(source, cert, session, seen=seen)
        return cert

    async def validate_authority(self, source, cert, session, *, seen=None):
        resource = await session.resource(source.id)
        require(
            resource.type == 'delegation' and resource.state == 'active',
            'authority_source_inactive',
        )
        require(source.revision == resource.revision, 'authority_source_changed')
        # The controlled delegation facts are distinct from mutable ordinary content.
        fact = session.setting('delegation:' + source.id)
        require(fact is not None and fact['grantee'] == cert.subject_id, 'authority_source_invalid')
        from msg.core.codec import decode, parse_time
        from msg.core.models import CapabilityGrant

        require(parse_time(fact['expires_at']) > self.clock(), 'authority_source_expired')
        for raw in fact['grants']:
            grant = decode(CapabilityGrant, raw)
            target = await session.resource(grant.scope.resource_id)
            require(target.owner == fact['grantor'], 'authority_source_lost')
        allowed = tuple(decode(CapabilityGrant, g) for g in fact['grants'])
        for grant in (*cert.grants, *(cert.issuance.issue_grants if cert.issuance else ())):
            require(
                any([
                    g.capability == grant.capability
                    and g.version == grant.version
                    and grant.operations <= g.operations
                    and await scope_subset(grant.scope, g.scope, session)
                    and constraints_subset(grant.constraints, g.constraints)
                    for g in allowed
                ]),
                'delegation_scope_exceeded',
            )
        if fact.get('parent_certificate'):
            await self.validate(
                fact['parent_certificate'], session, seen=set(seen or ()) | {cert.resource_id}
            )
        return fact

    async def validate_publication(self, cert, csr, session):
        verify_csr(csr)
        require(
            cert.subject_id == csr.subject_id
            and cert.issuer_id == csr.requested_issuer
            and cert.key_id == key_id(csr.public_key)
            and cert.kind == csr.kind
            and cert.grants == csr.grants
            and cert.issuance == csr.issuance
            and cert.authority_sources == csr.authority_sources
            and cert.delegation_depth == csr.delegation_depth
            and cert.target_service == csr.target_service,
            'csr_certificate_mismatch',
        )
        require(
            (cert.expires_at - cert.not_before).total_seconds() <= csr.requested_ttl_seconds,
            'csr_ttl_exceeded',
        )
        return await self.validate(
            cert.resource_id, session, certificate=cert, issuance_resource=csr.resource_id
        )
