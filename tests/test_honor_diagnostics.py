import pytest
from test_service import call, register

from msg.bootstrap import feature_manifest, manifest
from msg.core.codec import wire
from msg.core.errors import Failure


def test_honor_feature_has_non_granting_bootstrap_sample():
    row = next(row for row in feature_manifest() if row['feature_id'] == 'honors')
    assert row['sample_resource'] == 'r_honor_sample'
    assert row['doctor_check'] == 'honors'
    assert row['selftest_case'] == 'honor_ceremony_display'
    sample = manifest()['honor_sample']
    assert sample['profile']['pinned_grant_ids'] == []
    assert sample['grants'] == []
    assert sample['issuer']['purpose'] == 'achievement-grant'


@pytest.mark.asyncio
async def test_honor_diagnostics_use_installed_sample_and_real_requests(installed):
    from msg.admin.honor_check import check_honors, inspect_honors
    app, _ = installed
    async with app.metadata.transaction(write=False) as tx:
        assert (await inspect_honors(app, tx))['read_only'] is True
        assert tx.one('SELECT COUNT(*) FROM achievement_grants')[0] == 0
    sample = await call(app, 'discovery.get', {'id': 'r_honor_sample'})
    assert sample.status == 'ok'
    assert wire(sample.data['content']) == manifest()['honor_sample']

    async def invoke(name, args, key=None, subject=None):
        return await call(app, name, args, key=key, subject=subject)

    async def enrolled(name):
        key, subject, _ = await register(app, name)
        return key, subject

    assert await check_honors(app, invoke, enrolled)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM achievement_grants')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM achievement_pins')[0] == 0
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('ALTER TABLE achievement_pins RENAME COLUMN position TO broken_position', write=True)
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='honor_schema_missing'):
            await inspect_honors(app, tx)
