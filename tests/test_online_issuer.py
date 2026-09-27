"""Online issuance uses live authority facts and leaves an attributable audit."""
import pytest
from types import SimpleNamespace
from datetime import timedelta

from msg.core.codec import b64, canonical, loads, wire, digest
from msg.core.errors import Failure
from msg.core.models import Scope, IssuancePolicy
from msg.security.capabilities import grant_for
from msg.security.crypto import Ed25519Signer
from msg.plugins.identity import issue_online
from msg.core.requests import request_for
from test_authorization import request_certificate
from test_service import NOW, call, register


async def issuance_audit(app, certificate_id):
    async with app.metadata.transaction(write=False) as tx:
        rows = [loads(raw) for (raw,) in tx.execute('SELECT body FROM audit ORDER BY seq')]
    return next(row for row in rows if row['event']['type']=='cert.auto.issue' and
                row['event']['data']['certificate_id']==certificate_id)


@pytest.mark.asyncio
async def test_possession_based_registration_and_owner_delegation_audit_real_sources(installed):
    app, _ = installed
    owner_key, owner, owner_cert = await register(app, 'issuer-owner')
    other_key, other, _ = await register(app, 'issuer-other')
    registration = await issuance_audit(app, owner_cert)
    assert registration['event']['data']['automatic'] is True
    assert registration['event']['data']['policy_version'] == 1
    assert registration['event']['data']['issuer'] == 'u_online_ca'
    assert registration['event']['actor'] == owner
    assert registration['event']['data']['authority_source']['kind'] == 'possession_proof'
    assert registration['event']['data']['authority_source']['key_id'] == owner_key.key_id

    post = await call(app, 'content.post_create', {'parent':'/main','body':'delegatable'},
                      key=owner_key, subject=owner)
    grant = grant_for(app.registry.capability('discovery.basic'),
                      scope=Scope(resource_id=post.resources[0].id),
                      operations=('discovery.get@1',))
    delegated = await call(app, 'identity.delegate',
                           {'grantee':other,'key_id':other_key.key_id,
                            'grants':wire((grant,)),'ttl':600},
                           key=owner_key, subject=owner, rid='delegate-audit')
    assert delegated.status=='ok', wire(delegated)
    audit = await issuance_audit(app, delegated.data['certificate_id'])
    source_id = delegated.resources[0].id
    assert audit['event']['data']['automatic'] is True
    assert audit['event']['actor']==owner and audit['event']['subject']==other
    assert audit['event']['request_id']=='delegate-audit'
    assert audit['event']['data']['authority_source']['id']==source_id
    assert audit['event']['data']['grant_digest']==digest((grant,))


@pytest.mark.asyncio
async def test_renewal_preserves_or_narrows_live_certificate_authority(installed):
    app, _ = installed
    key, owner, original_id = await register(app, 'renew-owner')
    same = await call(app, 'identity.certificate_renew', {}, key=key, subject=owner)
    assert same.status=='ok', wire(same)
    async with app.metadata.transaction(write=False) as tx:
        original = await tx.certificate(original_id)
        renewed = await tx.certificate(same.data['certificate_id'])
    assert renewed.grants==original.grants
    audit = await issuance_audit(app, renewed.resource_id)
    assert audit['event']['data']['authority_source']['certificate_id']==original_id

    narrow = original.grants[0]
    narrow = type(narrow)(capability=narrow.capability,version=narrow.version,
                          scope=narrow.scope,operations=frozenset({next(iter(narrow.operations))}),
                          constraints=narrow.constraints)
    reduced = await call(app, 'identity.certificate_renew',
                         {'grants':wire((narrow,)),'ttl':600}, key=key, subject=owner)
    assert reduced.status=='ok', wire(reduced)
    async with app.metadata.transaction(write=False) as tx:
        child = await tx.certificate(reduced.data['certificate_id'])
    assert child.grants==(narrow,)
    assert (child.expires_at-child.not_before).total_seconds()==600
    denied = await call(app, 'identity.certificate_renew', {'ttl':31536000},
                        key=key, subject=owner)
    assert denied.error is not None and denied.error.code=='renewal_ttl_exceeded', wire(denied)


@pytest.mark.asyncio
async def test_revoked_renewal_source_cannot_be_issued_again(installed):
    app, _ = installed
    key, owner, original_id = await register(app, 'renew-revoked')
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE certificates SET revoked=1 WHERE id=?',(original_id,),write=True)
    denied = await call(app, 'identity.certificate_renew', {}, key=key, subject=owner)
    assert denied.error is not None and denied.error.code=='renewal_source_required', wire(denied)


@pytest.mark.asyncio
async def test_renewal_cannot_extend_past_the_live_source_window(installed):
    app, _ = installed
    key, owner, original_id = await register(app, 'renew-window')
    async with app.metadata.transaction(write=False) as tx:
        source = await tx.certificate(original_id)
    later = NOW + timedelta(days=1)
    app.clock = lambda: later
    app.authenticator.clock = app.clock
    app.certificates.clock = app.clock
    app.executor.clock = app.clock

    def renewal(arguments):
        return request_for('identity.certificate_renew', arguments, app.settings.service_url,
                           subject=owner, signer=key, expires_at=later + timedelta(seconds=120))

    requested = int((source.expires_at - source.not_before).total_seconds())
    denied = await app.executor.execute(renewal({'ttl':requested}))
    assert denied.error is not None and denied.error.code == 'renewal_ttl_exceeded', wire(denied)
    accepted = await app.executor.execute(renewal({}))
    assert accepted.status == 'ok', wire(accepted)
    async with app.metadata.transaction(write=False) as tx:
        successor = await tx.certificate(accepted.data['certificate_id'])
    assert successor.expires_at <= source.expires_at


@pytest.mark.asyncio
async def test_renewal_cannot_expand_operations_or_scope_after_broad_source_is_revoked(installed):
    app, _ = installed
    key, owner, original_id = await register(app, 'renew-narrow')
    async with app.metadata.transaction(write=False) as tx:
        original = await tx.certificate(original_id)
    grant = next(g for g in original.grants if g.capability=='discovery.basic')
    narrow = type(grant)(capability=grant.capability,version=grant.version,
                         scope=Scope(resource_id='t_main'),
                         operations=frozenset({'discovery.get@1'}),constraints=grant.constraints)
    first = await call(app, 'identity.certificate_renew', {'grants':wire((narrow,))},
                       key=key, subject=owner)
    assert first.status=='ok', wire(first)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE certificates SET revoked=1 WHERE id=?',(original_id,),write=True)
    broader_ops = type(narrow)(capability=narrow.capability,version=narrow.version,
                               scope=narrow.scope,operations=grant.operations,
                               constraints=narrow.constraints)
    operations = await call(app, 'identity.certificate_renew',
                            {'grants':wire((broader_ops,))}, key=key, subject=owner)
    assert operations.error.code=='renewal_scope_exceeded', wire(operations)
    broader_scope = type(narrow)(capability=narrow.capability,version=narrow.version,
                                 scope=grant.scope,operations=narrow.operations,
                                 constraints=narrow.constraints)
    scope = await call(app, 'identity.certificate_renew',
                       {'grants':wire((broader_scope,))}, key=key, subject=owner)
    assert scope.error.code=='renewal_scope_exceeded', wire(scope)


@pytest.mark.asyncio
async def test_online_issuer_rejects_special_delegation_and_keeps_ca_request_pending(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'special-requester')
    other_key, other, _ = await register(app, 'special-grantee')
    special=grant_for(app.registry.capability('system.inspect'))
    rejected=await call(app, 'identity.delegate',
                        {'grantee':other,'key_id':other_key.key_id,
                         'grants':wire((special,)),'ttl':600},key=key,subject=owner)
    assert rejected.error.code=='special_delegation_requires_ca', wire(rejected)
    issue=grant_for(app.registry.capability('cert.issue'))
    csr=await request_certificate(app, owner, key, key, (issue,), kind='ca',
        issuance=IssuancePolicy(issue_grants=(),max_cert_ttl_seconds=600,
                                max_child_ca_depth=0,max_delegation_depth=0))
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.csr_state(csr.data['csr_id'])).status=='pending'
        assert tx.one('SELECT COUNT(*) FROM certificates WHERE subject=?',(owner,))[0]==1


@pytest.mark.asyncio
async def test_expired_delegation_source_blocks_another_automatic_signature(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'expiry-owner')
    other_key, other, _ = await register(app, 'expiry-grantee')
    post = await call(app, 'content.post_create', {'parent':'/main','body':'scope'},
                      key=owner_key, subject=owner)
    grant = grant_for(app.registry.capability('discovery.basic'),
                      scope=Scope(resource_id=post.resources[0].id),
                      operations=('discovery.get@1',))
    delegated = await call(app, 'identity.delegate',
                           {'grantee':other,'key_id':other_key.key_id,
                            'grants':wire((grant,)),'ttl':600},key=owner_key,subject=owner)
    assert delegated.status=='ok', wire(delegated)
    async with app.metadata.transaction(write=True) as tx:
        certificate = await tx.certificate(delegated.data['certificate_id'])
        source = certificate.authority_sources[0]
        fact = tx.setting('delegation:'+source.id)
        tx.set_setting('delegation:'+source.id,
                       {**fact,'expires_at':wire(NOW-timedelta(seconds=1))})
        context = SimpleNamespace(now=NOW,principal=SimpleNamespace(actor=owner))
        request = SimpleNamespace(request_id='expired-source-reissue')
        with pytest.raises(Failure, match='authority_source_expired'):
            await issue_online(app,tx,other,other_key.key_id,context,request,
                               grants=(grant,),kind='delegation',sources=(source,))


@pytest.mark.asyncio
async def test_added_key_receives_only_its_requested_basic_certificate_grants(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'key-ceiling-owner')
    added_key = Ed25519Signer.generate()
    narrow = grant_for(app.registry.capability('discovery.basic'),
                       scope=Scope(resource_id='t_main'),operations=('discovery.get@1',))
    public=b64(added_key.public_key)
    proof=added_key.sign(canonical({'subject_id':owner,'public_key':public}),purpose='key-add')
    created = await call(app, 'identity.key_add',
                         {'public_key':public,
                          'possession_proof':wire(proof),'ceiling':wire((narrow,))},
                         key=owner_key,subject=owner)
    assert created.status=='ok', wire(created)
    async with app.metadata.transaction(write=False) as tx:
        certificate=await tx.certificate(created.data['certificate_id'])
    assert certificate.grants==(narrow,)
