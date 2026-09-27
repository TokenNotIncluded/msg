from dataclasses import replace
from datetime import timedelta
import os
import pytest

from msg.admin.root import _approve_csr
from msg.constants import ROOT_SUBJECT,ROOT_SPACE
from msg.core.codec import canonical,wire,digest,b64,unb64,decode
from msg.core.models import CertificateRequest,CapabilityGrant,Scope,Signature,ResourceRef,Credential,Certificate,IssuancePolicy,Event,AuditEvent
from msg.security.certificates import csr_body,sign_certificate
from msg.security.crypto import Ed25519Signer
from msg.security.capabilities import grant_for
from msg.plugins.identity import certificate_resource
from test_service import call,register,NOW,temporary_v3_args


async def request_certificate(app,uid,login_key,subject_key,grants, *, kind='capability',issuance=None,issuer=ROOT_SUBJECT,depth=0,ttl=3600):
    csr=CertificateRequest(resource_id='server-generated',applicant=uid,subject_id=uid,requested_issuer=issuer,
        public_key=subject_key.public_key,kind=kind,grants=tuple(grants),issuance=issuance,requested_ttl_seconds=ttl,
        target_service=app.settings.service_url,delegation_depth=depth,authority_sources=(),request_digest='',
        possession_proof=Signature(key_id=subject_key.key_id,algorithm='ed25519',value=b''))
    proof=subject_key.sign(canonical(csr_body(csr)),purpose='csr')
    args={'requested_issuer':issuer,'public_key':b64(subject_key.public_key),'kind':kind,'grants':wire(tuple(grants)),
        'issuance':wire(issuance),'requested_ttl_seconds':ttl,'delegation_depth':depth,'possession_proof':wire(proof)}
    result=await call(app,'cert.request',args,key=login_key,subject=uid)
    assert result.status=='ok',wire(result)
    return result


async def approve(app,root,uid,key,grants, **kwargs):
    csr=await request_certificate(app,uid,key,key,grants,**kwargs)
    return await _approve_csr(app,csr.data['csr_id'],root,expected_digest=csr.data['request_digest'],operator='test-console')


def scoped(app,cap,rid,operations,desc=False):
    return grant_for(app.registry.capability(cap),scope=Scope(resource_id=rid,descendants=desc),operations=operations)


@pytest.mark.asyncio
async def test_certificate_gate_exact_operation_scope_and_revoke(installed):
    app,root=installed
    key,uid,base=await register(app,'cap-agent')
    grant=scoped(app,'resource.certified_write','t_certified',('content.post_create@1',),True)
    cert=await approve(app,root,uid,key,(grant,))
    create=await call(app,'content.post_create',{'parent':'/certified','body':'certified'},key=key,subject=uid,certs=(cert.resource_id,))
    assert create.status=='ok',wire(create)
    # Additional gate does not imply chmod, even for a resource owned by the caller.
    denied=await call(app,'content.chmod',{'id':create.resources[0].id,'mode':'0777'},key=key,subject=uid,
        certs=(cert.resource_id,),expected=((create.resources[0].id,create.data['generation']),))
    assert denied.error.code=='certificate_gate',wire(denied)
    # A different scope cannot grant access to the gate.
    wrong=await approve(app,root,uid,key,(scoped(app,'resource.certified_write','t_main',('content.post_create@1',),True),))
    denied=await call(app,'content.post_create',{'parent':'/certified','body':'wrong'},key=key,subject=uid,certs=(wrong.resource_id,))
    assert denied.error.code=='certificate_gate',wire(denied)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE certificates SET revoked=1 WHERE id=?',(cert.resource_id,),write=True)
    revoked=await call(app,'content.post_create',{'parent':'/certified','body':'revoked'},key=key,subject=uid,certs=(cert.resource_id,))
    assert revoked.error.code=='certificate_revoked',wire(revoked)


@pytest.mark.asyncio
async def test_sticky_setgid_and_current_membership(installed):
    app,root=installed
    ka,ua,ca=await register(app,'alice')
    kb,ub,cb=await register(app,'bob')
    post=await call(app,'content.post_create',{'parent':'/tmp','body':'belongs to Alice'},key=ka,subject=ua)
    rejected=await call(app,'content.archive',{'id':post.resources[0].id},key=kb,subject=ub,
                        expected=((post.resources[0].id,post.data['generation']),))
    assert rejected.error.code=='sticky_denied',wire(rejected)
    cert=await approve(app,root,ua,ka,(scoped(app,'group.manage_override','g_admins',('group.member.add@1','group.member.remove@1')),))
    joined=await call(app,'group.member.add',{'group':'/&admins','subject':ub},key=ka,subject=ua,certs=(cert.resource_id,))
    assert joined.status=='ok',wire(joined)
    created=await call(app,'content.topic_create',{'parent':'/admins','name':'nested'},key=kb,subject=ub)
    assert created.status=='ok',wire(created)
    async with app.metadata.transaction(write=False) as tx:
        r=await tx.resource(created.resources[0].id)
        assert r.group=='g_admins' and r.mode&0o2000
    removed=await call(app,'group.member.remove',{'group':'/&admins','subject':ub},key=ka,subject=ua,certs=(cert.resource_id,))
    assert removed.status=='ok',wire(removed)
    denied=await call(app,'discovery.get',{'id':created.resources[0].id},key=kb,subject=ub)
    assert denied.error.code=='permission_denied',wire(denied)


@pytest.mark.asyncio
async def test_replay_does_not_leak_changed_private_content_or_create_again(installed):
    app,_=installed
    ka,ua,ca=await register(app,'alice')
    kb,ub,cb=await register(app,'bob')
    post=await call(app,'content.post_create',{'parent':'/main','body':'visible'},key=ka,subject=ua)
    args={'recipient':ua,'resource':wire(post.resources[0])}
    sent=await call(app,'communication.send',args,key=kb,subject=ub,rid='one-delivery')
    assert sent.status=='ok',wire(sent)
    lock=await call(app,'content.chmod',{'id':post.resources[0].id,'mode':'0600'},key=ka,subject=ua,
                    expected=((post.resources[0].id,post.data['generation']),))
    assert lock.status=='ok',wire(lock)
    again=await call(app,'communication.send',args,key=kb,subject=ub,rid='one-delivery')
    assert again.error and again.error.code=='permission_denied',wire(again)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM messages')[0]==1


@pytest.mark.asyncio
async def test_key_id_cannot_be_reassigned_by_temporary_upgrade(installed):
    app,_=installed
    key,uid,base=await register(app,'permanent')
    temp_args,temp_rid,_,_=temporary_v3_args()
    tmp=await call(app,'identity.temporary',temp_args,rid=temp_rid,contract_version=3)
    tempid=tmp.data['subject_id']
    from msg.security.age_keys import generate_age_key
    _,recipient=generate_age_key()
    args={'subject_id':tempid,'public_key':b64(key.public_key),'handle':'other',
          'encryption_recipient':recipient}
    result=await call(app,'identity.upgrade',{'handle':'other','public_key':args['public_key'],
        'encryption_recipient':recipient,
        'possession_proof':wire(key.sign(canonical(args),purpose='upgrade'))},subject=tempid,
        token=(tmp.data['credential_id'],unb64(tmp.data['token'])),contract_version=2)
    assert result.error.code=='credential_identity_immutable',wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.credential(key.key_id)).subject_id==uid
        assert (await tx.subject(tempid)).kind=='temporary'


@pytest.mark.asyncio
async def test_token_rotation_retry_releases_once_and_revokes_old_credential(installed):
    app,_=installed
    temp_args,temp_rid,_,_=temporary_v3_args()
    tmp=await call(app,'identity.temporary',temp_args,rid=temp_rid,contract_version=3)
    uid=tmp.data['subject_id']
    token=(tmp.data['credential_id'],unb64(tmp.data['token']))
    nonce=b64(os.urandom(32))
    recovery_secret=b64(os.urandom(32))
    assert nonce!=recovery_secret
    args={'nonce':nonce,'recovery_secret':recovery_secret}
    first=await call(app,'identity.token_rotate',args,subject=uid,token=token,
                     rid='rotate-once',contract_version=2)
    assert first.status=='ok',wire(first)
    again=await call(app,'identity.token_rotate',args,subject=uid,token=token,
                     rid='rotate-once',contract_version=2)
    assert again.error.code=='token_delivery_unavailable',wire(again)
    denied=await call(app,'content.post_create',{'parent':'/main','body':'old credential'},subject=uid,token=token)
    assert denied.error.code=='credential_revoked',wire(denied)


@pytest.mark.asyncio
async def test_csr_key_does_not_grant_shell_or_generic_login(installed):
    app,root=installed
    key,uid,base=await register(app,'agent-ca')
    ca_key=Ed25519Signer.generate()
    issue=scoped(app,'cert.issue',ROOT_SPACE,('cert.publish@1','cert.get@1','discovery.get@1'),True)
    can_issue=scoped(app,'resource.certified_write','t_certified',('content.post_create@1',),True)
    request=await request_certificate(app,uid,key,ca_key,(issue,),kind='ca',
        issuance=IssuancePolicy(issue_grants=(can_issue,),max_cert_ttl_seconds=3600,max_child_ca_depth=0,max_delegation_depth=0))
    approved=await _approve_csr(app,request.data['csr_id'],root,expected_digest=request.data['request_digest'],operator='test-console')
    # The approved CA key has exactly the requested use ceiling, not the account's full ceiling.
    denied=await call(app,'content.post_create',{'parent':'/main','body':'not an unrestricted login'},key=ca_key,subject=uid,
                      certs=(approved.resource_id,))
    assert denied.error.code=='credential_ceiling',wire(denied)

@pytest.mark.asyncio
async def test_special_ceiling_does_not_turn_into_ordinary_owner_rights(installed):
    app,root=installed
    owner,uid,base=await register(app,'ceiling-owner')
    delegated=Ed25519Signer.generate()
    cap=scoped(app,'resource.read_override','t_main',('content.post_create@1',),True)
    proof=delegated.sign(canonical({'subject_id':uid,'public_key':b64(delegated.public_key)}),purpose='key-add')
    added=await call(app,'identity.key_add',{'public_key':b64(delegated.public_key),'possession_proof':wire(proof),
        'ceiling':wire((cap,))},key=owner,subject=uid)
    assert added.status=='ok',wire(added)
    rejected=await call(app,'content.post_create',{'parent':'/main','body':'read cap must not permit writing'},key=delegated,subject=uid)
    assert rejected.status=='error',wire(rejected)
