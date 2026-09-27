"""Context rebase and multi-file patches use one PostgreSQL commit boundary."""
import asyncio

import pytest

from msg.core.codec import wire
from msg.core.models import ResourceRef
from test_service import call, register


async def snapshot(app, ids):
    async with app.metadata.transaction(write=False) as tx:
        resources=[await tx.resource(rid) for rid in ids]
        return (tuple((resource.generation,resource.revision) for resource in resources),
                tx.one('SELECT COUNT(*) FROM revisions')[0],
                tx.one('SELECT COUNT(*) FROM events')[0])


async def body(app, rid):
    async with app.metadata.transaction(write=False) as tx:
        revision=await tx.revision(ResourceRef(id=rid))
        return (await app.contents.read_bytes(revision.content)).decode('utf-8')


@pytest.mark.asyncio
async def test_rebase_unique_context_and_reject_changed_or_ambiguous(installed):
    app,_=installed
    key,subject,_=await register(app,'rebase-owner')
    created=await call(app,'content.post_create',{'parent':'/main','body':'alpha one\nbeta two\n'},key=key,subject=subject)
    rid=created.resources[0].id
    base=created.resources[0].revision
    first=await call(app,'content.text_patch',{'id':rid,'base_revision':base,
        'exact':'alpha','replacement':'ALPHA'},key=key,subject=subject,
        expected=((rid,created.data['generation']),))
    assert first.status=='ok',wire(first)
    stale={'id':rid,'base_revision':base,'base_generation':created.data['generation'],
           'rebase':True,'exact':'beta','replacement':'BETA','after':' two'}
    rebased=await call(app,'content.text_patch',stale,key=key,subject=subject,
        expected=((rid,first.data['generation']),),contract_version=2)
    assert rebased.status=='ok',wire(rebased)
    assert await body(app,rid)=='ALPHA one\nBETA two\n'
    before=await snapshot(app,(rid,))
    changed=await call(app,'content.text_patch',{**stale,'exact':'alpha'},key=key,subject=subject,
        expected=((rid,rebased.data['generation']),),contract_version=2)
    assert changed.error.code=='patch_no_match',wire(changed)
    bad_generation=await call(app,'content.text_patch',{**stale,'base_generation':rebased.data['generation']},
        key=key,subject=subject,expected=((rid,rebased.data['generation']),),contract_version=2)
    assert bad_generation.error.code=='base_generation_conflict',wire(bad_generation)
    assert await snapshot(app,(rid,))==before


@pytest.mark.asyncio
async def test_batch_preflight_failure_and_concurrent_writers_publish_nothing_partial(installed):
    app,_=installed
    key,subject,_=await register(app,'batch-patcher')
    a=await call(app,'content.post_create',{'parent':'/main','body':'first A'},key=key,subject=subject)
    b=await call(app,'content.post_create',{'parent':'/main','body':'second B'},key=key,subject=subject)
    ids=(a.resources[0].id,b.resources[0].id)
    expected=((ids[0],a.data['generation']),(ids[1],b.data['generation']))
    patches=[{'id':ids[0],'base_revision':a.resources[0].revision,'base_generation':a.data['generation'],
              'exact':'A','replacement':'AA'},
             {'id':ids[1],'base_revision':b.resources[0].revision,'base_generation':b.data['generation'],
              'exact':'absent','replacement':'BB'}]
    before=await snapshot(app,ids)
    rejected=await call(app,'content.text_patch_batch',{'patches':patches},key=key,subject=subject,expected=expected)
    assert rejected.error.code=='patch_no_match',wire(rejected)
    assert await snapshot(app,ids)==before
    patches[1]['exact']='B'
    first,second=await asyncio.gather(*(
        call(app,'content.text_patch_batch',{'patches':patches},key=key,subject=subject,
             expected=expected) for _ in range(2)))
    assert sorted((first.status,second.status))==['error','ok'],(wire(first),wire(second))
    assert (first.error or second.error).code=='generation_conflict'
    assert await body(app,ids[0])=='first AA'
    assert await body(app,ids[1])=='second BB'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM revisions WHERE resource_id IN (?,?)',ids)[0]==4


@pytest.mark.asyncio
async def test_batch_second_revision_failure_rolls_back_pg(monkeypatch,installed):
    app,_=installed
    key,subject,_=await register(app,'batch-rollback')
    created=[await call(app,'content.post_create',{'parent':'/main','body':body},
                        key=key,subject=subject) for body in ('one X','two Y')]
    ids=tuple(item.resources[0].id for item in created)
    before=await snapshot(app,ids)
    patches=[{'id':item.resources[0].id,'base_revision':item.resources[0].revision,
              'base_generation':item.data['generation'],'exact':needle,'replacement':'changed'}
             for item,needle in zip(created,('X','Y'))]
    original=app.contents.commit_revision
    calls=0
    async def fail_second(topic,revision):
        nonlocal calls
        calls+=1
        if calls==2:
            raise RuntimeError('injected second revision failure')
        return await original(topic,revision)
    monkeypatch.setattr(app.contents,'commit_revision',fail_second)
    result=await call(app,'content.text_patch_batch',{'patches':patches},key=key,subject=subject,
        expected=tuple((item.resources[0].id,item.data['generation']) for item in created))
    assert result.error.code=='internal_error',wire(result)
    assert await snapshot(app,ids)==before
    assert await body(app,ids[0])=='one X' and await body(app,ids[1])=='two Y'
