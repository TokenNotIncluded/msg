"""Request authorization and immutable content signatures are separate proofs."""
import uuid
import pytest
from msg.core.codec import canonical, wire
from msg.core.models import Revision
from msg.security.crypto import verify
from test_service import NOW, call, register


async def signed_arguments(app, key, subject, *, body='A considered statement.',
                           resource_id=None, parent_revision=None, kind='soul'):
    rid=resource_id or 'r_'+uuid.uuid4().hex
    vid='v_'+uuid.uuid4().hex
    blob=await app.contents.put_bytes(body.encode(), 'text/markdown')
    revision=Revision(format_version=1,id=vid,resource_id=rid,
        parents=(parent_revision,) if parent_revision else (),content=blob,relations=(),
        actor=subject,subject=subject,author=subject,created_at=NOW,manifest_digest='')
    manifest={k:v for k,v in wire(revision).items() if k not in {'manifest_digest','signature'}}
    args={'kind':kind,'body':body,'resource_id':rid,'revision_id':vid,
          'content_created_at':wire(NOW),
          'content_signature':wire(key.sign(canonical(manifest),purpose='revision'))}
    if parent_revision:
        args['expected_revision']=parent_revision
    return args, manifest


@pytest.mark.asyncio
async def test_personal_v2_preserves_independently_signed_history(installed):
    app,_=installed
    key,subject,_=await register(app,'signed-personal')
    first,manifest=await signed_arguments(app,key,subject)
    saved=await call(app,'identity.personal_put',first,key=key,subject=subject,contract_version=2)
    assert saved.status=='ok',wire(saved)
    ref=saved.resources[0]
    async with app.metadata.transaction(write=False) as tx:
        old=await tx.revision(ref)
        verify(key.public_key,canonical(manifest),old.signature,purpose='revision')
        assert (await tx.resource(ref.id)).mode==0o600
    second,new_manifest=await signed_arguments(app,key,subject,resource_id=ref.id,
        parent_revision=ref.revision,body='A new statement, not a rewrite of history.')
    updated=await call(app,'identity.personal_put',second,key=key,subject=subject,
        expected=((ref.id,saved.data['generation']),),contract_version=2)
    assert updated.status=='ok',wire(updated)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.revision(ref)==old
        current=await tx.revision(updated.resources[0])
        verify(key.public_key,canonical(new_manifest),current.signature,purpose='revision')
    second['body']='Changed after the content signature was made.'
    rejected=await call(app,'identity.personal_put',second,key=key,subject=subject,
        expected=((ref.id,updated.data['generation']),),contract_version=2)
    assert rejected.status=='error'


@pytest.mark.asyncio
async def test_bad_content_signature_rolls_back_personal_creation(installed):
    app,_=installed
    key,subject,_=await register(app,'signed-rejection')
    args,_=await signed_arguments(app,key,subject)
    args['body']='Tampered body.'
    result=await call(app,'identity.personal_put',args,key=key,subject=subject,contract_version=2)
    assert result.status=='error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM resources WHERE id=?',(args['resource_id'],)) is None
        assert tx.one('SELECT 1 FROM revisions WHERE id=?',(args['revision_id'],)) is None
