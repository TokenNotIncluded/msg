"""User CA keys stay on the client; the service verifies published certificates."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from msg.atomic_file import durable_write
from msg.core.codec import canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import (
    CapabilityGrant,
    Certificate,
    CertificateRequest,
    IssuancePolicy,
    Signature,
)
from msg.security.certificates import csr_body, sign_certificate, verify_csr
from msg.security.crypto import Ed25519Signer, key_id


def local_key(path, *, create=False):
    path = Path(path)
    if not path.exists() and create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = Ed25519Signer.generate()
        durable_write(path, key.private_bytes(), mode=0o600)
        return key
    require(
        path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0,
        'unsafe_ca_key_permissions',
    )
    return Ed25519Signer.from_bytes(path.read_bytes())


async def request_certificate(client, spec, key_path=None):
    require(
        client.state.subject and client.state.signer is not None, 'registered_identity_required'
    )
    require(
        set(spec)
        <= {
            'requested_issuer',
            'kind',
            'grants',
            'issuance',
            'requested_ttl_seconds',
            'delegation_depth',
        },
        'unknown_csr_field',
    )
    issuer = spec.get('requested_issuer', 'u_root')
    if issuer.startswith('/'):
        result = client.checked(
            await client.call('discovery.get', {'id': issuer, 'fields': ['id']}, anonymous=True)
        )
        issuer = result.data['id']
    signer = (
        local_key(key_path or client.state.directory / 'ca.key', create=True)
        if spec.get('kind') == 'ca' or key_path
        else client.state.signer
    )
    csr = CertificateRequest(
        resource_id='client-pending',
        applicant=client.state.subject,
        subject_id=client.state.subject,
        requested_issuer=issuer,
        public_key=signer.public_key,
        kind=spec.get('kind', 'capability'),
        grants=tuple(decode(CapabilityGrant, g) for g in spec.get('grants', ())),
        issuance=decode(IssuancePolicy, spec['issuance']) if spec.get('issuance') else None,
        requested_ttl_seconds=spec.get('requested_ttl_seconds', 86400),
        target_service=client.state.server,
        delegation_depth=spec.get('delegation_depth', 0),
        authority_sources=(),
        request_digest='',
        possession_proof=Signature(key_id=signer.key_id, algorithm='ed25519', value=b''),
    )
    proof = signer.sign(canonical(csr_body(csr)), purpose='csr')
    args = {
        k: v
        for k, v in wire(csr).items()
        if k
        in {
            'requested_issuer',
            'public_key',
            'kind',
            'grants',
            'issuance',
            'requested_ttl_seconds',
            'delegation_depth',
        }
    }
    args['possession_proof'] = wire(proof)
    journal = client.state.directory / ('csr-' + digest(csr_body(csr))[7:39] + '.json')
    pending = loads(journal.read_bytes()) if journal.exists() else {'request_id': uuid4().hex}
    durable_write(journal, canonical(pending), mode=0o600)
    result = await client.call('cert.request', args, request_id=pending['request_id'])
    if result.status == 'ok':
        durable_write(
            journal,
            canonical({
                **pending,
                'csr_id': result.data['csr_id'],
                'request_digest': result.data['request_digest'],
            }),
            mode=0o600,
        )
    return result


async def issue_certificate(
    client, csr_id, issuer_certificate_id, key_path, *, expected_digest=None
):
    signer = local_key(key_path)
    issuer_result = client.checked(
        await client.call('cert.get', {'id': issuer_certificate_id}, anonymous=True)
    )
    parent = decode(Certificate, issuer_result.data['certificate'])
    require(
        parent.kind == 'ca' and parent.issuance is not None and parent.key_id == signer.key_id,
        'not_matching_ca_key',
    )
    require(
        parent.subject_id == client.state.subject and parent.subject_id != 'u_root', 'local_only'
    )
    certificates = (issuer_certificate_id,)
    result = client.checked(
        await client.call('cert.get', {'id': csr_id}, signer=signer, certificates=certificates)
    )
    csr = decode(CertificateRequest, result.data['request'])
    verify_csr(csr)
    require(csr.requested_issuer == parent.subject_id, 'wrong_requested_issuer')
    # The review binds the exact immutable request, not a mutable title or path.
    print(
        canonical({'request': wire(csr), 'issuer': issuer_certificate_id}).decode(), file=sys.stderr
    )
    if expected_digest is None:
        require(sys.stdin.isatty(), 'approval_digest_required')
        expected_digest = input('Type the exact CSR digest to approve: ').strip()
    require(expected_digest == csr.request_digest, 'approval_digest_mismatch')
    if result.data['state']['status'] == 'issued':
        return await client.call(
            'cert.get', {'id': result.data['state']['certificate_id']}, anonymous=True
        )
    require(result.data['state']['status'] == 'pending', 'csr_not_pending')
    journal = client.state.directory / (
        'issued-' + digest((csr_id, issuer_certificate_id))[7:39] + '.json'
    )
    if journal.exists():
        pending = loads(journal.read_bytes())
        require(pending['csr_digest'] == csr.request_digest, 'approval_digest_mismatch')
        cert = decode(Certificate, pending['certificate'])
    else:
        now = client.clock()
        require(parent.not_before <= now < parent.expires_at, 'issuer_expired')
        require(
            csr.requested_ttl_seconds <= parent.issuance.max_cert_ttl_seconds,
            'certificate_ttl_escalation',
        )
        cert = Certificate(
            resource_id='cert_' + uuid4().hex,
            serial='serial_' + uuid4().hex,
            subject_id=csr.subject_id,
            key_id=key_id(csr.public_key),
            issuer_id=parent.subject_id,
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
        cert = sign_certificate(cert, signer)
        pending = {
            'csr_digest': csr.request_digest,
            'certificate': wire(cert),
            'request_id': uuid4().hex,
        }
        durable_write(journal, canonical(pending), mode=0o600)
    # Server revalidates issuance scope, operations, revocation, expiry, authority
    # sources and remaining depth in the committing SQLite transaction.
    return await client.call(
        'cert.publish',
        {'csr_id': csr_id, 'certificate': wire(cert)},
        signer=signer,
        certificates=certificates,
        request_id=pending['request_id'],
    )
