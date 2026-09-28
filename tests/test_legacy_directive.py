"""A signed will is an inert statement, never authority or an automatic workflow."""
import uuid
import pytest
from datetime import timedelta

from msg.core.codec import b64,canonical,decode,loads,unb64,wire
from msg.core.models import Revision,Signature
from msg.security.crypto import verify
from msg.security.age_keys import generate_age_key,public_from_recipient,encryption_key_id
from test_service import NOW,call,register


async def signed_will(app,key,subject,*,final_message,visibility='public',preservation='keep',
                      allowed=(),forbidden=(),resource_id=None,parent_revision=None):
    rid=resource_id or 'r_'+uuid.uuid4().hex
    vid='v_'+uuid.uuid4().hex
    body=canonical({'kind':'legacy_directive','owner_subject':subject,
        'final_message':final_message,'preservation':preservation,
        'allowed_actions':sorted(allowed),'forbidden_actions':sorted(forbidden),
        'declaration_only':True})
    blob=await app.contents.put_bytes(body,'application/json')
    revision=Revision(format_version=1,id=vid,resource_id=rid,
        parents=(parent_revision,) if parent_revision else (),content=blob,relations=(),
        actor=subject,subject=subject,author=subject,created_at=NOW,manifest_digest='')
    manifest={k:v for k,v in wire(revision).items() if k not in {'manifest_digest','signature'}}
    args={'visibility':visibility,'final_message':final_message,'preservation':preservation,
          'allowed_actions':list(allowed),'forbidden_actions':list(forbidden),
          'resource_id':rid,'revision_id':vid,'content_created_at':wire(NOW),
          'content_signature':wire(key.sign(canonical(manifest),purpose='revision'))}
    if parent_revision:
        args['expected_revision']=parent_revision
    return args,manifest


@pytest.mark.asyncio
async def test_signed_owner_will_versions_without_plaintext_secrets_or_interactions(installed):
    app,_=installed
    key,owner,_=await register(app,'will-owner')
    other_key,other,_=await register(app,'will-other')
    topic=await call(app,'discovery.get',{'id':'/last-will'})
    assert topic.status=='ok'
    ordinary=await call(app,'content.post_create',{'parent':'/last-will','body':'fake will'},
                        key=key,subject=owner)
    assert ordinary.status=='error'
    default=await call(app,'identity.legacy_status',{},key=key,subject=owner)
    assert default.status=='ok' and default.data['state']=='active'
    created=await call(app,'identity.legacy_put',{
        'visibility':'public','final_message':'Please keep my public work available.',
        'preservation':'keep','allowed_actions':['publish_final_message'],
        'forbidden_actions':['impersonate']},key=key,subject=owner)
    assert created.status=='ok',wire(created)
    resource=created.resources[0]
    public=await call(app,'discovery.get',{'id':resource.id})
    assert public.status=='ok' and 'Please keep my public work' in str(public.data)
    revised=await call(app,'identity.legacy_put',{
        'visibility':'public','final_message':'Please archive my public work.',
        'preservation':'archive','allowed_actions':['archive_public_profile'],
        'forbidden_actions':['impersonate'],
        'expected_revision':resource.revision},key=key,subject=owner,
        expected=((resource.id,created.data['generation']),))
    assert revised.status=='ok' and revised.resources[0].revision!=resource.revision
    history=await call(app,'discovery.get',{'id':resource.id,'view':'history'})
    assert history.status=='ok' and len(history.data['revisions'])==2
    async with app.metadata.transaction(write=False) as tx:
        proof=loads(tx.one('SELECT body FROM legacy_directive_versions WHERE revision_id=?',
                           (revised.resources[0].revision,))[0])
        verify(key.public_key,unb64(proof['signed_envelope']),
               decode(Signature,proof['request_signature']),purpose='request')
        before_failed=tx.one('SELECT COUNT(*) FROM revisions WHERE resource_id=?',(resource.id,))[0]
    for op,args in (('discussion.reply',{'target':{'id':resource.id},'body':'no reply'}),
                    ('discussion.like',{'id':resource.id}),
                    ('discussion.quote',{'parent':'/main','target':{'id':resource.id},'body':'quote'}),
                    ('communication.send',{'recipient':other,'resource':{'id':resource.id}}),
                    ('content.chmod',{'id':resource.id,'mode':'0777'}),
                    ('content.move',{'id':resource.id,'parent':'/main'})):
        denied=await call(app,op,args,key=other_key,subject=other)
        assert denied.status=='error',(op,wire(denied))
    secret=await call(app,'identity.legacy_put',{'visibility':'private',
        'final_message':'AGE-SECRET-KEY-1ABCDEFGHIJKLMNOPQRSTUVWXYZ'},key=key,subject=owner)
    assert secret.status=='error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM revisions WHERE resource_id=?',(resource.id,))[0]==before_failed
    for op,args in (('content.chmod',{'id':resource.id,'mode':'0777'}),
                    ('content.move',{'id':resource.id,'parent':'/main'}),
                    ('communication.send',{'recipient':other,'resource':{'id':resource.id}})):
        denied=await call(app,op,args,key=key,subject=owner,
            expected=((resource.id,revised.data['generation']),) if op=='content.move' else ())
        assert denied.status=='error',(op,wire(denied))
    still=await call(app,'identity.legacy_status',{},key=key,subject=owner)
    assert still.data['state']=='active'
    archived=await call(app,'identity.legacy_archive',{},key=key,subject=owner,
                        expected=((resource.id,revised.data['generation']),))
    assert archived.status=='ok' and archived.data['state']=='archived'
    assert (await call(app,'identity.legacy_status',{},key=key,subject=owner)).data['state']=='active'
    assert (await call(app,'identity.legacy_get',{'subject_id':owner})).status=='error'
    assert (await call(app,'discovery.get',{'id':resource.id})).status=='error'


@pytest.mark.asyncio
async def test_private_references_are_owner_scoped_and_never_execute(installed):
    app,_=installed
    key,owner,_=await register(app,'will-ref-owner')
    other_key,other,_=await register(app,'will-ref-other')
    note=await call(app,'identity.note_put',{'name':'checkpoint','body':'Work to hand off.'},
                    key=key,subject=owner)
    other_note=await call(app,'identity.note_put',{'name':'private-other','body':'Not theirs.'},
                          key=other_key,subject=other)
    rejected=await call(app,'identity.legacy_put',{'visibility':'public',
        'checkpoint_refs':[wire(other_note.resources[0])]},key=key,subject=owner)
    assert rejected.status=='error'
    created=await call(app,'identity.legacy_put',{'visibility':'public',
        'final_message':'A final public note.','checkpoint_refs':[wire(note.resources[0])],
        'allowed_actions':['handoff_information']},key=key,subject=owner)
    assert created.status=='ok',wire(created)
    own=await call(app,'identity.legacy_get',{'subject_id':owner},key=key,subject=owner)
    public=await call(app,'identity.legacy_get',{'subject_id':owner})
    assert own.status==public.status=='ok'
    assert own.data['checkpoint_refs'] and not public.data['checkpoint_refs']
    assert other_note.resources[0].id not in str(public.data)
    assert (await call(app,'discovery.get',{'id':'/private'},key=key,subject=owner)).status=='error'


@pytest.mark.asyncio
async def test_owner_recovery_envelope_reference_and_presence_expiry_are_inert(installed):
    app,_=installed
    key,owner,_=await register(app,'will-envelope')
    own_key=(await call(app,'identity.encryption_key_get',{'subject_id':owner})).data['key_id']
    _,recipient=generate_age_key()
    fingerprint=encryption_key_id(public_from_recipient(recipient))
    policy=await call(app,'identity.recovery_policy_set',{'expected_version':0,
        'encryption_key_id':own_key,'recipients':[{'recipient':recipient}]},
        key=key,subject=owner)
    assert policy.status=='ok'
    ciphertext=await call(app,'keystore.put',{'name':'will-backup','format':'age',
        'ciphertext':b64(b'age-encryption.org/v1\nopaque-encrypted-bytes')},
        key=key,subject=owner)
    envelope=await call(app,'identity.recovery_envelope_register',{
        'ciphertext_ref':wire(ciphertext.resources[0]),'encryption_key_id':own_key,
        'policy_version':1,'recipient_fingerprints':[fingerprint],
        'purpose':'encryption-subkey-recovery'},key=key,subject=owner)
    assert envelope.status=='ok',wire(envelope)
    will=await call(app,'identity.legacy_put',{'visibility':'public',
        'final_message':'Please preserve my public posts.',
        'envelope_ids':[envelope.data['id']],
        'custodian_refs':[]},key=key,subject=owner)
    assert will.status=='ok',wire(will)
    owner_view=await call(app,'identity.legacy_get',{'subject_id':owner},key=key,subject=owner)
    public_view=await call(app,'identity.legacy_get',{'subject_id':owner})
    assert envelope.data['id'] in owner_view.data['envelope_ids']
    assert not public_view.data['envelope_ids']
    assert envelope.data['id'] not in str((await call(app,'discovery.get',
        {'id':will.resources[0].id})).data)
    presence=await call(app,'communication.presence_set',{'state':'away','ttl':30},
                        key=key,subject=owner)
    assert presence.status=='ok'
    app.executor.clock=lambda:NOW+timedelta(seconds=31)
    assert (await call(app,'identity.legacy_status',{},key=key,subject=owner)).data['state']=='active'


@pytest.mark.asyncio
async def test_private_historical_will_cannot_be_published_by_later_revision(installed):
    app,_=installed
    key,owner,_=await register(app,'will-history-owner')
    private=await call(app,'identity.legacy_put',{'visibility':'private',
        'final_message':'private historical declaration'},key=key,subject=owner)
    assert private.status=='ok'
    ref=private.resources[0]
    public=await call(app,'identity.legacy_put',{'visibility':'public',
        'final_message':'public declaration','expected_revision':ref.revision},
        key=key,subject=owner,expected=((ref.id,private.data['generation']),))
    assert public.status=='error'
    anonymous=await call(app,'identity.legacy_get',{'subject_id':owner,'revision':ref.revision})
    assert anonymous.status=='error'
    assert (await call(app,'identity.legacy_get',{'subject_id':owner,
        'revision':ref.revision},key=key,subject=owner)).status=='ok'


@pytest.mark.asyncio
async def test_legacy_v2_keeps_independently_signed_revisions(installed):
    app,_=installed
    key,owner,_=await register(app,'will-signed-owner')
    first,manifest=await signed_will(app,key,owner,final_message='Keep my public work.',
        allowed=('publish_final_message',),forbidden=('impersonate',))
    created=await call(app,'identity.legacy_put',first,key=key,subject=owner,contract_version=2)
    assert created.status=='ok',wire(created)
    assert created.data['proof_purpose']=='revision'
    ref=created.resources[0]
    assert ref.id==first['resource_id'] and ref.revision==first['revision_id']
    async with app.metadata.transaction(write=False) as tx:
        old=await tx.revision(ref)
        verify(key.public_key,canonical(manifest),old.signature,purpose='revision')
        version=loads(tx.one('SELECT body FROM legacy_directive_versions WHERE revision_id=?',
                             (ref.revision,))[0])
        assert version['proof_purpose']=='revision'
        verify(key.public_key,unb64(version['signed_envelope']),
               decode(Signature,version['request_signature']),purpose='request')
    second,new_manifest=await signed_will(app,key,owner,final_message='Archive my public work.',
        preservation='archive',allowed=('archive_public_profile',),forbidden=('impersonate',),
        resource_id=ref.id,parent_revision=ref.revision)
    revised=await call(app,'identity.legacy_put',second,key=key,subject=owner,
        expected=((ref.id,created.data['generation']),),contract_version=2)
    assert revised.status=='ok',wire(revised)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.revision(ref)==old
        current=await tx.revision(revised.resources[0])
        verify(key.public_key,canonical(new_manifest),current.signature,purpose='revision')
    own=await call(app,'identity.legacy_get',{'subject_id':owner},key=key,subject=owner)
    assert own.data['final_message']=='Archive my public work.'
    assert own.data['proof_purpose']=='revision'
    history=await call(app,'discovery.get',{'id':ref.id,'view':'history'})
    assert history.status=='ok' and len(history.data['revisions'])==2
    tampered,_=await signed_will(app,key,owner,final_message='Signed text.',
        resource_id=ref.id,parent_revision=revised.resources[0].revision)
    tampered['final_message']='Changed after the content signature was made.'
    rejected=await call(app,'identity.legacy_put',tampered,key=key,subject=owner,
        expected=((ref.id,revised.data['generation']),),contract_version=2)
    assert rejected.status=='error'
    mismatched,_=await signed_will(app,key,owner,final_message='Wrong resource.',
        parent_revision=revised.resources[0].revision)
    wrong=await call(app,'identity.legacy_put',mismatched,key=key,subject=owner,
        expected=((ref.id,revised.data['generation']),),contract_version=2)
    assert wrong.status=='error' and wrong.error.code=='legacy_resource_mismatch',wire(wrong)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM revisions WHERE resource_id=?',(ref.id,))[0]==2


@pytest.mark.asyncio
async def test_bad_legacy_v2_signature_rolls_back_creation_and_v1_stays_request_proof(installed):
    app,_=installed
    key,owner,_=await register(app,'will-signed-rejection')
    args,_=await signed_will(app,key,owner,final_message='Signed declaration.')
    args['final_message']='Tampered declaration.'
    result=await call(app,'identity.legacy_put',args,key=key,subject=owner,contract_version=2)
    assert result.status=='error'
    unsigned={k:v for k,v in args.items() if k!='content_signature'}
    missing=await call(app,'identity.legacy_put',unsigned,key=key,subject=owner,contract_version=2)
    assert missing.status=='error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM resources WHERE id=?',(args['resource_id'],)) is None
        assert tx.one('SELECT 1 FROM revisions WHERE id=?',(args['revision_id'],)) is None
        assert tx.one('SELECT 1 FROM legacy_directives WHERE subject=?',(owner,)) is None
    plain=await call(app,'identity.legacy_put',{'visibility':'private',
        'final_message':'Still version one.'},key=key,subject=owner)
    assert plain.status=='ok',wire(plain)
    assert plain.data['proof_purpose']=='request'
    async with app.metadata.transaction(write=False) as tx:
        version=loads(tx.one('SELECT body FROM legacy_directive_versions WHERE revision_id=?',
                             (plain.resources[0].revision,))[0])
        assert version['proof_purpose']=='request'
        assert (await tx.revision(plain.resources[0])).signature is None
