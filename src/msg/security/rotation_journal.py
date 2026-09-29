"""Validate a locally approved root rotation without loading a network service."""

from msg.constants import ONLINE_CA, ROOT_SUBJECT
from msg.core.codec import canonical, decode, digest, unb64
from msg.core.errors import require
from msg.core.models import Certificate, CertificateRequest, Signature
from msg.security.certificates import certificate_body, verify_csr
from msg.security.crypto import Ed25519Signer, open_private_key, verify

_FIELDS = {
    'version',
    'old_certificate',
    'new_trust',
    'new_envelope',
    'online_csr',
    'previous_envelope_digest',
    'statement',
    'new_signature',
    'old_signature',
}


def authorization(journal):
    """Public, signed commitments; never copy the encrypted root into an audit."""
    return {
        'version': journal['version'],
        'statement': journal['statement'],
        'old_certificate_digest': digest(journal['old_certificate']),
        'new_trust_digest': digest(journal['new_trust']),
        'new_envelope_digest': digest(journal['new_envelope']),
        'online_csr_digest': digest(journal['online_csr']),
        'previous_envelope_digest': journal['previous_envelope_digest'],
    }


def validate(journal, *, pin, service, online_public):
    require(
        isinstance(journal, dict) and journal.get('version') == 2,
        'rotation_journal_upgrade_required',
    )
    require(set(journal) == _FIELDS, 'invalid_rotation_journal')
    signer = Ed25519Signer.from_bytes(open_private_key(journal['new_envelope'], pin))
    trust = journal['new_trust']
    require(
        set(trust) == {'version', 'public_key', 'certificate'} and trust['version'] == 1,
        'invalid_trust_anchor',
    )
    require(signer.public_key == unb64(trust['public_key'], limit=32), 'root_key_mismatch')
    proof = authorization(journal)
    verify(
        signer.public_key,
        canonical(proof),
        decode(Signature, journal['new_signature']),
        purpose='root-rotation',
    )
    old = decode(Certificate, journal['old_certificate'])
    new = decode(Certificate, trust['certificate'])
    statement = journal['statement']
    require(
        statement['old_certificate'] == old.resource_id
        and statement['new_certificate'] == new.resource_id
        and statement['new_fingerprint'] == digest(signer.public_key)
        and type(statement['previous_key_proved']) is bool
        and statement['previous_key_proved'] == (journal['old_signature'] is not None),
        'rotation_statement_mismatch',
    )
    require(
        old.resource_id != new.resource_id and old.key_id != new.key_id, 'rotation_keys_must_change'
    )
    require(
        new.key_id == signer.key_id
        and new.subject_id == new.issuer_id == ROOT_SUBJECT
        and new.parent_certificate_id is None
        and new.kind == 'ca'
        and new.issuance is not None
        and not new.authority_sources
        and old.subject_id == ROOT_SUBJECT
        and new.target_service == old.target_service == service,
        'rotation_service_mismatch',
    )
    verify(
        signer.public_key, canonical(certificate_body(new)), new.signature, purpose='certificate'
    )
    csr = decode(CertificateRequest, journal['online_csr'])
    verify_csr(csr)
    require(
        csr.public_key == online_public
        and csr.subject_id == csr.applicant == ONLINE_CA
        and csr.requested_issuer == ROOT_SUBJECT
        and csr.target_service == service
        and csr.kind == 'ca'
        and csr.delegation_depth == 0
        and not csr.authority_sources
        and csr.issuance is not None
        and csr.issuance.max_child_ca_depth == 0
        and len(csr.grants) == 1
        and csr.grants[0].capability == 'cert.issue',
        'rotation_online_csr_mismatch',
    )
    return signer, old, new, csr, proof
