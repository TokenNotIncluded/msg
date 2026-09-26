from datetime import timedelta
from dataclasses import replace
import hashlib
from pathlib import Path
import pytest

from msg.core.codec import wire
from msg.core.errors import Failure
from msg.admin.diagnostics import doctor, selftest
from msg.workers.maintenance import run_maintenance
from test_service import register, call, NOW
from test_authorization import approve, scoped


@pytest.mark.asyncio
async def test_network_system_configuration_cannot_change_root_trust(installed):
    app,root=installed
    key,uid,base=await register(app,'operator')
    denied=await call(app,'system.config',{'values':{'accept_writes':False}},key=key,subject=uid)
    assert denied.error.code=='capability_required',wire(denied)
    cert=await approve(app,root,uid,key,(scoped(app,'system.config','r_root',('system.config@1',)),))
    ok=await call(app,'system.config',{'values':{'accept_writes':False}},key=key,subject=uid,certs=(cert.resource_id,))
    assert ok.status=='ok',wire(ok)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('runtime_config')['accept_writes'] is False
    bad=await call(app,'system.config',{'values':{'root_private_key':'/tmp/key'}},key=key,subject=uid,certs=(cert.resource_id,))
    assert bad.status=='error'
    blocked=await call(app,'content.post_create',{'parent':'/main','body':'capacity is paused'},key=key,subject=uid)
    assert blocked.error.code=='writes_paused'
    reading=await call(app,'discovery.get',{'id':'/main'})
    assert reading.status=='ok'


@pytest.mark.asyncio
async def test_policy_cleanup_is_explicit_records_tombstone_and_does_not_delete_other_topics(installed):
    app,root=installed
    key,uid,base=await register(app,'temporary-content')
    old=await call(app,'content.post_create',{'parent':'/tmp','body':'expires'},key=key,subject=uid)
    keep=await call(app,'content.post_create',{'parent':'/main','body':'retained'},key=key,subject=uid)
    app.clock=lambda: NOW+timedelta(hours=2)
    result=await run_maintenance(app,'cleanup_expired',scheduled=True)
    assert result['expired_resources']==1,result
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(old.resources[0].id)).state=='purged'
        assert (await tx.resource(keep.resources[0].id)).state=='active'
        assert tx.one('SELECT COUNT(*) FROM audit')[0]>=2
    reading=await call(app,'discovery.get',{'id':old.resources[0].id})
    assert reading.error.code=='resource_purged'


@pytest.mark.asyncio
async def test_doctor_does_not_create_database_or_repair_bootstrap(installed,tmp_path):
    app,root=installed
    status=doctor(app.settings.config_dir,clock=lambda:NOW)
    assert status['checks']['bootstrap']['ok'],status
    assert status['checks']['root_trust']['ok'],status
    missing=tmp_path/'missing'
    result=doctor(missing)
    assert not result['ok'] and not missing.exists()
    async with app.metadata.transaction(write=True) as tx:
        r=await tx.resource('t_private')
        await tx.replace(replace(r,mode=0o777,generation=r.generation+1),r.generation)
    drift=doctor(app.settings.config_dir,clock=lambda:NOW)
    assert not drift['checks']['bootstrap']['ok']
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource('t_private')).mode==0o777


@pytest.mark.asyncio
async def test_selftest_isolated_end_to_end_checks():
    result=await selftest()
    assert result['ok'],result
    assert result['checks']['sticky'] and result['checks']['certgate'] and result['checks']['root_network_rejected']
    assert result['cleaned_up']
