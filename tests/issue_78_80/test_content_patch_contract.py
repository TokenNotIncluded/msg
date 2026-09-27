from dataclasses import replace
import pytest

from msg.client_content import signed_patch_arguments
from msg.core.codec import canonical,wire,digest
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.security.crypto import verify


@pytest.mark.parametrize('kind,operation,version', [
    ('file','content.text_patch',3),('post','content.post_patch',2)])
async def test_independent_signature_and_old_bytes(harness,kind,operation,version):
    h=harness
    resource=await h.create('r_document',type=kind,body='# Title\nold\n')
    async with h.app.metadata.transaction(write=False) as tx:
        old=await tx.revision(ResourceRef(id=resource.id))
        old_bytes=canonical(old)
    patch={'kind':'heading','heading':'Title','digest':old.content.digest,
           'replacement':'# Title\nnew\n'}
    args=signed_patch_arguments(h.signer,resource,old,'# Title\nold\n',patch,
        subject='u_alice',created_at=h.ctx.now,change_note='Update only this section')
    result=await h.invoke(operation,args,version,expected=((resource.id,resource.generation),))
    async with h.app.metadata.transaction(write=False) as tx:
        revision=await tx.revision(result.resources[0])
        assert canonical(await tx.revision(ResourceRef(id=resource.id,revision=old.id)))==old_bytes
        assert await h.app.contents.read_bytes(old.content)==b'# Title\nold\n'
        assert await h.app.contents.read_bytes(revision.content)==b'# Title\nnew\n'
        assert revision.change_note=='Update only this section'
        assert revision.source_digest==digest(patch)
        assert revision.source_kind=='user'
        manifest={k:v for k,v in wire(revision).items() if k not in {'manifest_digest','signature'}}
        assert revision.manifest_digest==digest(manifest)
        verify(h.signer.public_key,canonical(manifest),revision.signature,purpose='revision')
        assert tx.one('SELECT text FROM projections WHERE resource_id=?',(resource.id,))==('# Title\nnew\n',)


async def test_signature_failure_rolls_back_all_batch_sql_refs(harness):
    h=harness
    resources=[await h.create('r_'+str(i),type='file',body='old\n') for i in range(2)]
    args=[]
    async with h.app.metadata.transaction(write=False) as tx:
        before=tx.rows('SELECT id,revision,generation FROM resources ORDER BY id')
        before_revisions=tx.rows('SELECT id,body FROM revisions ORDER BY id')
        before_index=tx.rows('SELECT * FROM projections ORDER BY resource_id')
        for resource in resources:
            rev=await tx.revision(ResourceRef(id=resource.id))
            args.append(signed_patch_arguments(h.signer,resource,rev,'old\n',
                {'kind':'exact','exact':'old','replacement':'new'},subject='u_alice',created_at=h.ctx.now))
    # Valid request, but the second independently signed manifest is tampered.
    args[1]['change_note']='not covered by the original signature'
    with pytest.raises(Failure) as exc:
        await h.invoke('content.text_patch_batch',{'patches':args},2,
            expected=tuple((r.id,r.generation) for r in resources))
    assert exc.value.code=='invalid_signature'
    async with h.app.metadata.transaction(write=False) as tx:
        assert tx.rows('SELECT id,revision,generation FROM resources ORDER BY id')==before
        assert tx.rows('SELECT id,body FROM revisions ORDER BY id')==before_revisions
        assert tx.rows('SELECT * FROM projections ORDER BY resource_id')==before_index


@pytest.mark.parametrize('cause,code', [('generation','generation_conflict'),
    ('revision','revision_conflict'),('private','permission_denied'),('archived','not_editable')])
async def test_edit_preconditions(harness,cause,code):
    h=harness
    resource=await h.create('r_edit',type='file',body='old\n')
    args={'id':resource.id,'base_revision':resource.revision,'base_generation':resource.generation,
          'patch':{'kind':'exact','exact':'old','replacement':'new'}}
    if cause=='revision':args['base_revision']='v_wrong'
    if cause in {'private','archived'}:
        async with h.app.metadata.transaction(write=True) as tx:
            updated=replace(resource,generation=resource.generation+1,
                **({'mode':0,'owner':'u_other'} if cause=='private' else {'state':'archived'}))
            await tx.replace(updated,resource.generation)
            resource=updated
    expected=resource.generation-1 if cause=='generation' else resource.generation
    with pytest.raises(Failure) as exc:
        await h.invoke('content.text_patch',args,3,expected=((resource.id,expected),))
    assert exc.value.code==code


async def test_legacy_exact_and_structured_schema_stay_separate(harness):
    h=harness
    resource=await h.create('r_old',type='file',body='old\n')
    args={'id':resource.id,'base_revision':resource.revision,'exact':'old','replacement':'new'}
    result=await h.invoke('content.text_patch',args,1,expected=((resource.id,resource.generation),))
    async with h.app.metadata.transaction(write=False) as tx:
        revision=await tx.revision(result.resources[0])
        assert revision.source_digest is None  # Never reinterpret the published v1 contract.
    with pytest.raises(Failure) as exc:
        await h.invoke('content.text_patch',args,3)
    assert exc.value.code=='schema_validation'


async def test_schema_validation_happens_before_content_write(harness,monkeypatch):
    h=harness
    resource=await h.create('r_bad',type='file',body='old\n')
    async def unexpected(*args,**kwargs):
        pytest.fail('schema failure must not write content')
    monkeypatch.setattr(h.app.contents,'put_bytes',unexpected)
    with pytest.raises(Failure):
        await h.invoke('content.text_patch',{'id':resource.id,'base_revision':resource.revision,
            'base_generation':resource.generation,'patch':{'kind':'block','digest':'short',
                'replacement':'new'}},3,expected=((resource.id,resource.generation),))


async def test_legacy_batch_keeps_exact_published_response(harness):
    h=harness
    resource=await h.create('r_legacy_batch',type='file',body='old\n')
    result=await h.execute('content.text_patch_batch',{'patches':[{
        'id':resource.id,'base_revision':resource.revision,'base_generation':resource.generation,
        'exact':'old','replacement':'new'}]},expected=((resource.id,resource.generation),))
    assert result.error is None
    assert wire(result.data)=={'generations':{resource.id:resource.generation+1}}
