from dataclasses import replace
from datetime import UTC, datetime, timedelta
import pytest

from msg.core.codec import canonical, decode, digest, freeze_json, loads, wire
from msg.core.errors import Failure
from msg.core.models import Resource, ResourceId, BlobRef, ResourceRef
from msg.storage.sqlite import SqliteMetadataStore, FakeMetadataStore
from msg.storage.git import GitContentStore


def item(id='one', parent=None):
    now = datetime.now(UTC)
    return Resource(id=ResourceId(id), type='topic', type_version=1, name=id,
        parent=parent, owner=ResourceId('owner'), group=ResourceId('group'), mode=0o1777,
        generation=0, revision=None, state='active', created_at=now, created_by=ResourceId('owner'),
        modified_at=now, modified_by=ResourceId('owner'))


def test_canonical_is_stable_and_rejects_ambiguous_json():
    assert canonical({'b': 1, 'a': [2]}) == b'{"a":[2],"b":1}'
    for raw in ['{"a":1,"a":2}', '{"n":NaN}', '{"n":Infinity}', '{"n":1e999}']:
        with pytest.raises(Failure): loads(raw)
    with pytest.raises(Failure): canonical({'n':float('nan')})
    with pytest.raises(Failure): decode(ResourceRef, {'id':'x','extra':1})
    with pytest.raises(Failure): decode(BlobRef, {'digest':'sha256:x','size':True,'media_type':'text/plain'})


def test_recursive_freeze_no_aliases():
    original={'a':[{'b':2}]}
    frozen=freeze_json(original)
    original['a'][0]['b']=3
    assert frozen['a'][0]['b']==2
    with pytest.raises(TypeError): frozen['a'][0]['b']=4


def test_resource_roundtrip_and_validation():
    r=item()
    assert decode(Resource, wire(r))==r
    assert wire(r)['mode']=='1777'
    with pytest.raises(Failure): replace(r, mode=True)
    with pytest.raises(Failure): replace(r, generation=-1)
    with pytest.raises(Failure): replace(r, created_at=datetime(2026,1,1))
    with pytest.raises(Failure): replace(r, state='unknown')


@pytest.mark.parametrize('store_class', [SqliteMetadataStore, FakeMetadataStore])
async def test_transaction_rollback_and_generation(tmp_path, store_class):
    store=store_class(tmp_path/'metadata.db')
    with pytest.raises(RuntimeError):
        async with store.transaction(write=True) as tx:
            await tx.insert(item())
            raise RuntimeError('rollback')
    async with store.transaction(write=True) as tx:
        with pytest.raises(Failure, match='not_found'): await tx.resource('one')
        await tx.insert(item())
    async with store.transaction(write=True) as tx:
        await tx.replace(replace(await tx.resource('one'), generation=1, mode=0o700),0)
        with pytest.raises(Failure, match='generation_conflict'):
            await tx.replace(replace(item(),generation=1),0)
    async with store.transaction(write=False) as tx:
        assert (await tx.resource('one')).mode==0o700
        with pytest.raises(Failure, match='read_only_transaction'): await tx.insert(item('other'))
    await store.close()


async def test_store_path_single_parent_and_aliases(tmp_path):
    store=SqliteMetadataStore(tmp_path/'db')
    async with store.transaction(write=True) as tx:
        await tx.insert(item('root'))
        await tx.insert(item('topic','root'))
        await tx.insert(item('post','topic'))
        assert await tx.resolve('/topic/post')=='post'
        assert await tx.resolve('/_id/post')=='post'
        with pytest.raises(Failure): await tx.resolve('/topic/../post')
        with pytest.raises(Failure): await tx.replace(replace(await tx.resource('root'),parent='post',generation=1),0)


async def test_git_preserves_original_bytes_and_pins(tmp_path):
    store=GitContentStore(tmp_path/'objects')
    raw=b'hello\r\n\xe4\xb8\xad\xe6\x96\x87\x00'
    async def chunks():
        yield raw[:4]
        yield raw[4:]
    ref=await store.put(chunks(),'application/octet-stream',digest(raw))
    await store.pin(ref,'test')
    assert b''.join([part async for part in store.read(ref)])==raw
    assert b''.join([part async for part in store.read(ref,(1,5))])==raw[1:5]
    assert await store.pinned(ref,'test')
    await store.unpin(ref,'test')
    assert not await store.pinned(ref,'test')
