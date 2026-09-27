"""Real restart/pg_dump/restore and readonly diagnostics for private market state."""
import asyncio

import pytest
from test_market_arbitration import configured, details, reasoned_base, vote
from test_service import NOW, call

from msg.admin.backups import backup, restore
from msg.admin.market_check import inspect_market
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import unb64, wire
from msg.core.errors import Failure
from msg.workers.effects import EffectWorker


async def snapshot(tx):
    tables=('store_orders','order_contracts','order_settlements','order_transitions',
        'arbitration_cases','arbitration_votes','arbitration_decisions','arbitration_evidence',
        'money_ledger','jobs','events','audit','results','settings')
    return {name:tx.rows('SELECT * FROM '+name+' ORDER BY 1') for name in tables}


@pytest.mark.asyncio
async def test_restart_and_backup_preserve_private_case_decision_without_reexecution(installed,tmp_path,pg_dsn):
    app,_root,members,sk,seller,bk,buyer,_order,opened=await configured(installed)
    cid=opened['case_id']; text='Private sealed dispute evidence. Never public.'
    stored=await call(app,'orders.dispute_statement',{'case_id':cid,'visibility':'panel','text':text},key=bk,subject=buyer)
    assert stored.status=='ok',wire(stored)
    eid=stored.data['evidence_id']
    arb=opened['panel'][0]
    reason=await reasoned_base(app,members[arb],arb,cid)
    data=await details(app,bk,buyer,cid)
    reason_id=next(row['id'] for row in data['evidence'] if row['kind']=='rationale')
    for uid in opened['panel'][:2]:
        cast=await vote(app,members[uid],uid,reason)
    assert cast.status=='ok' and cast.data['decision'],wire(cast)
    args={'case_id':cid,'decision_id':cast.data['decision']['id']}
    # A new Application reads the same authoritative metadata; no in-memory panel.
    restarted=Application(app.settings,clock=lambda:NOW)
    await restarted.load()
    try:
        result=await call(restarted,'orders.dispute_execute',args,key=bk,subject=buyer)
        assert result.status=='ok',wire(result)
    finally:
        await restarted.close()
    archive=tmp_path/'market.zip'
    await backup(app,archive)
    async with app.metadata.transaction(write=False) as tx:
        original=await snapshot(tx)
        report=await inspect_market(tx)
        assert report['orders']==1
        assert await snapshot(tx)==original
    restored_config=tmp_path/'restored-etc'; restored_data=tmp_path/'restored-data'
    await asyncio.to_thread(restore,archive,restored_config,restored_data,postgres_dsn=pg_dsn)
    restored=Application(load_settings(restored_config),clock=lambda:NOW)
    await restored.load()
    try:
        async with restored.metadata.transaction(write=False) as tx:
            await inspect_market(tx)
            # Recovery intentionally changes safety settings, not the ledger/case.
            after=await snapshot(tx)
            for table in set(original)-{'settings','results','events','audit'}:
                assert after[table]==original[table],table
        evidence=await call(restored,'orders.dispute_evidence_get',{'case_id':cid,'evidence_id':eid},key=bk,subject=buyer)
        assert evidence.status=='ok' and unb64(evidence.data['data']).decode()==text,wire(evidence)
        other=await call(restored,'orders.dispute_evidence_get',{'case_id':cid,'evidence_id':eid},key=sk,subject=seller)
        assert other.error.code=='evidence_not_found'
        rationale=await call(restored,'orders.dispute_evidence_get',
            {'case_id':cid,'evidence_id':reason_id},key=sk,subject=seller)
        assert rationale.status=='ok',wire(rationale)
        assert rationale.data['digest']==reason['rationale_digest']
        assert unb64(rationale.data['data'])==b'The signed allocation follows the case evidence.'
        assert await EffectWorker(restored).run_once() is False
        rejected=await call(restored,'orders.dispute_execute',args,key=bk,subject=buyer)
        assert rejected.error.code=='writes_paused'
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_market_doctor_cannot_mutate_or_synthesize_a_repair(installed):
    from test_market_lifecycle import buy, market
    app,root=installed
    _sk,_seller,bk,buyer,listing,_=await market(app,root)
    result=await buy(app,bk,buyer,listing)
    assert result.status=='ok'
    async with app.metadata.transaction(write=False) as tx:
        before=await snapshot(tx)
        assert (await inspect_market(tx))['escrow_invariants']
        assert await snapshot(tx)==before
    # Snapshot/settlement facts are append-only, even to a privileged DB session.
    with pytest.raises(Failure,match='constraint_conflict'):
        async with app.metadata.transaction(write=True) as tx:
            tx.execute("UPDATE order_contracts SET digest='forged'",write=True)
    async with app.metadata.transaction(write=False) as tx:
        assert await snapshot(tx)==before
