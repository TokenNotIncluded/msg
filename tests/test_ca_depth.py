"""The CA level is derived from its parent chain, never from a role label."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_authorization import request_certificate, scoped
from test_service import NOW, register

from msg.admin.root import _approve_csr
from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.core.models import Certificate, IssuancePolicy, Signature
from msg.plugins.identity import certificate_resource
from msg.security.certificates import sign_certificate


@pytest.mark.asyncio
async def test_root_only_issues_l1_with_at_most_two_more_ca_levels(installed):
    app, root = installed
    assert app.certificates.root_certificate.issuance.max_child_ca_depth == 3
    async with app.metadata.transaction(write=False) as tx:
        online = await tx.certificate(tx.setting('online_ca_certificate'))
    assert online.issuance.max_child_ca_depth == 0

    key, uid, _ = await register(app, 'depth-agent')
    issue = scoped(app, 'cert.issue', 'r_root', ('cert.publish@1',), True)
    ca_issue = scoped(app, 'cert.ca.issue', 'r_root', ('cert.publish@1',), True)
    policy = IssuancePolicy(
        issue_grants=(issue, ca_issue),
        max_cert_ttl_seconds=3600,
        max_child_ca_depth=3,
        max_delegation_depth=0,
    )
    csr = await request_certificate(
        app, uid, key, key, (issue, ca_issue), kind='ca', issuance=policy
    )
    with pytest.raises(Failure, match='ca_depth_exceeded'):
        await _approve_csr(
            app,
            csr.data['csr_id'],
            root,
            expected_digest=csr.data['request_digest'],
            operator='test-console',
        )


@pytest.mark.asyncio
async def test_legacy_root_depth_eight_cannot_extend_new_ca_tree(installed):
    app, root = installed
    current = app.certificates.root_certificate
    legacy = sign_certificate(
        replace(current, issuance=replace(current.issuance, max_child_ca_depth=8)), root
    )
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE certificates SET body=? WHERE id=?',
            (canonical(legacy).decode(), current.resource_id),
            write=True,
        )
    app.certificates.root_certificate = legacy
    key, uid, _ = await register(app, 'legacy-depth-agent')
    issue = scoped(app, 'cert.issue', 'r_root', ('cert.publish@1',), True)
    ca_issue = scoped(app, 'cert.ca.issue', 'r_root', ('cert.publish@1',), True)
    policy = IssuancePolicy(
        issue_grants=(issue, ca_issue),
        max_cert_ttl_seconds=3600,
        max_child_ca_depth=7,
        max_delegation_depth=0,
    )
    csr = await request_certificate(
        app, uid, key, key, (issue, ca_issue), kind='ca', issuance=policy
    )
    with pytest.raises(Failure, match='ca_depth_exceeded'):
        await _approve_csr(
            app,
            csr.data['csr_id'],
            root,
            expected_digest=csr.data['request_digest'],
            operator='test-console',
        )


@pytest.mark.asyncio
async def test_l1_l2_l3_chain_with_explicit_narrowing_validates(installed):
    app, root = installed
    issue = scoped(app, 'cert.issue', 'r_root', ('cert.publish@1',), True)
    ca_issue = scoped(app, 'cert.ca.issue', 'r_root', ('cert.publish@1',), True)
    grants = (issue, ca_issue)
    key, uid, _ = await register(app, 'l1-agent')
    l1_policy = IssuancePolicy(
        issue_grants=grants, max_cert_ttl_seconds=3600, max_child_ca_depth=2, max_delegation_depth=0
    )
    csr = await request_certificate(app, uid, key, key, grants, kind='ca', issuance=l1_policy)
    parent = await _approve_csr(
        app,
        csr.data['csr_id'],
        root,
        expected_digest=csr.data['request_digest'],
        operator='test-console',
    )
    parent_key = key
    for level, remaining in ((2, 1), (3, 0)):
        child_key, child_uid, _ = await register(app, f'l{level}-agent')
        policy = replace(l1_policy, max_child_ca_depth=remaining)
        child = Certificate(
            resource_id=f'cert_l{level}',
            serial=f'serial_l{level}',
            subject_id=child_uid,
            key_id=child_key.key_id,
            issuer_id=parent.subject_id,
            parent_certificate_id=parent.resource_id,
            authority_sources=(),
            kind='ca',
            grants=grants,
            not_before=NOW,
            expires_at=min(parent.expires_at, NOW + timedelta(seconds=3600)),
            target_service=app.settings.service_url,
            delegation_depth=0,
            issuance=policy,
            signature=Signature(key_id=parent_key.key_id, algorithm='ed25519', value=b''),
        )
        child = sign_certificate(child, parent_key)
        async with app.metadata.transaction(write=True) as tx:
            await certificate_resource(tx, child, NOW)
            await app.certificates.validate(child.resource_id, tx, certificate=child)
            await tx.register_certificate(child, None, 0)
        parent, parent_key = child, child_key
