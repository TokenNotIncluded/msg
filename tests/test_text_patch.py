"""Exact text patches keep a single authorized Revision boundary."""
import pytest

from msg.core.codec import b64, wire
from msg.core.models import ResourceRef
from test_service import call, register


async def state(app,rid):
    async with app.metadata.transaction(write=False) as tx:
        resource=await tx.resource(rid)
        return (resource.generation,resource.revision,
                tx.one('SELECT COUNT(*) FROM revisions')[0],
                tx.one('SELECT COUNT(*) FROM events')[0])


async def content(app,rid):
    async with app.metadata.transaction(write=False) as tx:
        revision=await tx.revision(ResourceRef(id=rid))
        return revision,(await app.contents.read_bytes(revision.content)).decode('utf-8')


@pytest.mark.asyncio
async def test_context_patch_post_and_file_preserve_revision_history(installed):
    app,_=installed
    key,owner,_=await register(app,'patch-owner')
    post=await call(app,'content.post_create',{'parent':'/main','body':'甲 fish\n乙 fish\n'},
                    key=key,subject=owner)
    assert post.status=='ok',wire(post)
    rid=post.resources[0].id
    old=post.resources[0].revision
    changed=await call(app,'content.text_patch',{'id':rid,'base_revision':old,
        'exact':'fish','replacement':'鱼','before':'乙 ','after':'\n'},
        key=key,subject=owner,expected=((rid,post.data['generation']),))
    assert changed.status=='ok',wire(changed)
    revision,body=await content(app,rid)
    assert body=='甲 fish\n乙 鱼\n'
    assert revision.parents==(old,)
    assert revision.content.media_type=='text/markdown'
    assert revision.actor==owner and revision.subject==owner and revision.manifest_digest

    file=await call(app,'content.file_put',{'parent':'/@patch-owner/files','name':'sample.txt',
        'data':b64('汉字 A'.encode()),'media_type':'text/plain'},key=key,subject=owner)
    assert file.status=='ok',wire(file)
    fid=file.resources[0].id
    result=await call(app,'content.text_patch',{'id':fid,'base_revision':file.resources[0].revision,
        'exact':'汉字','replacement':'文字'},key=key,subject=owner,
        expected=((fid,file.data['generation']),))
    assert result.status=='ok',wire(result)
    revision,body=await content(app,fid)
    assert body=='文字 A' and revision.content.media_type=='text/plain'


@pytest.mark.asyncio
async def test_patch_rejects_ambiguous_stale_missing_and_unauthorized_without_publication(installed):
    app,_=installed
    key,owner,_=await register(app,'patch-conflicts')
    other,outsider,_=await register(app,'patch-outsider')
    post=await call(app,'content.post_create',{'parent':'/main','body':'one one'},
                    key=key,subject=owner)
    rid=post.resources[0].id
    base=post.resources[0].revision
    generation=post.data['generation']
    original=await state(app,rid)
    cases=[
        (key,owner,{'id':rid,'base_revision':base,'exact':'one','replacement':'two'},generation,'patch_ambiguous'),
        (key,owner,{'id':rid,'base_revision':base,'exact':'absent','replacement':'two'},generation,'patch_no_match'),
        (key,owner,{'id':rid,'base_revision':'v_stale','exact':'one','replacement':'two'},generation,'revision_conflict'),
        (key,owner,{'id':rid,'base_revision':base,'exact':'one','replacement':'two'},generation+1,'generation_conflict'),
        (other,outsider,{'id':rid,'base_revision':base,'exact':'one','replacement':'two'},generation,'permission_denied'),
    ]
    for signer,subject,args,expected_generation,code in cases:
        failed=await call(app,'content.text_patch',args,key=signer,subject=subject,
                          expected=((rid,expected_generation),))
        assert failed.status=='error' and failed.error.code==code,wire(failed)
        assert await state(app,rid)==original


@pytest.mark.asyncio
async def test_patch_rejects_binary_and_bounded_complexity(installed):
    app,_=installed
    key,owner,_=await register(app,'patch-bounds')
    file=await call(app,'content.file_put',{'parent':'/@patch-bounds/files','name':'binary.bin',
        'data':b64(b'\xff\x00'),'media_type':'application/octet-stream'},key=key,subject=owner)
    fid=file.resources[0].id
    before=await state(app,fid)
    failed=await call(app,'content.text_patch',{'id':fid,'base_revision':file.resources[0].revision,
        'exact':'a','replacement':'b'},key=key,subject=owner,
        expected=((fid,file.data['generation']),))
    assert failed.status=='error' and failed.error.code=='text_patch_required',wire(failed)
    assert await state(app,fid)==before

    post=await call(app,'content.post_create',{'parent':'/main','body':'a'*5000},
                    key=key,subject=owner)
    rid=post.resources[0].id
    before=await state(app,rid)
    failed=await call(app,'content.text_patch',{'id':rid,'base_revision':post.resources[0].revision,
        'exact':'a','replacement':'b','before':'z'},key=key,subject=owner,
        expected=((rid,post.data['generation']),))
    assert failed.status=='error' and failed.error.code=='patch_too_complex',wire(failed)
    assert await state(app,rid)==before
