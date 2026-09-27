"""Money's fixed policy and editable labels stay separate from ledger facts."""

from dataclasses import replace

import pytest

from msg.admin.diagnostics import doctor
from msg.config import MoneyConfig, load_settings, write_example
from msg.core.errors import Failure
from test_service import NOW, call


def _config(tmp_path):
    directory=tmp_path/'etc'
    settings=write_example(directory,tmp_path/'data')
    return settings,directory/'msgd.toml'


def test_money_example_has_deterministic_defaults(tmp_path):
    settings,path=_config(tmp_path)
    assert settings.money==MoneyConfig()
    content=path.read_text()
    assert '[money]' in content
    for line in ('enabled = true','currency_id = "primary"',
                 'display_name = "MSG"','code = "MSG"','scale = 6',
                 'transfer_fee = 0','allow_overdraft = false'):
        assert line in content


def test_only_currency_labels_can_change(tmp_path):
    _,path=_config(tmp_path)
    path.write_text(path.read_text().replace('display_name = "MSG"',
        'display_name = "Community credits"').replace('code = "MSG"',
        'code = "CREDIT"'))
    money=load_settings(path.parent).money
    assert money.display_name=='Community credits' and money.code=='CREDIT'
    assert money.currency_id=='primary' and money.scale==6
    assert money.transfer_fee==0 and money.allow_overdraft is False


@pytest.mark.parametrize(('before','after','code'),[
    ('enabled = true','enabled = false','invalid_money_configuration'),
    ('currency_id = "primary"','currency_id = "secondary"','invalid_money_configuration'),
    ('scale = 6','scale = 2','invalid_money_configuration'),
    ('transfer_fee = 0','transfer_fee = 1','invalid_money_configuration'),
    ('allow_overdraft = false','allow_overdraft = true','invalid_money_configuration'),
    ('display_name = "MSG"','display_name = ""','invalid_money_display_name'),
    ('code = "MSG"','code = "msg"','invalid_money_code'),
    ('code = "MSG"','code = "MSG"\nBankRole = "alice"','unknown_money_configuration'),
    ('code = "MSG"','code = "MSG"\ntotal_supply = 100','unknown_money_configuration'),
    ('code = "MSG"','code = "MSG"\nServerOffer = "sale"','unknown_money_configuration'),
])
def test_money_rejects_policy_changes_and_business_facts(tmp_path,before,after,code):
    _,path=_config(tmp_path)
    path.write_text(path.read_text().replace(before,after))
    with pytest.raises(Failure) as error:
        load_settings(path.parent)
    assert error.value.code==code


@pytest.mark.asyncio
async def test_doctor_reports_money_config_without_claiming_market_e2e(installed):
    app,_=installed
    before=app.settings.money
    report=doctor(app.settings.config_dir,clock=lambda:NOW)
    assert report['checks']['money_config']=={
        'ok':True,'enabled':True,'currency_id':'primary',
        'display_name':'MSG','code':'MSG','scale':6,
        'transfer_fee':0,'allow_overdraft':False}
    assert load_settings(app.settings.config_dir).money==before
    assert 'money' not in report['features'] or report['features']['money']['status']!='pass'


@pytest.mark.asyncio
async def test_public_money_state_uses_configured_labels(installed):
    app,_=installed
    app.settings=replace(app.settings,money=replace(app.settings.money,
        display_name='Community credits',code='CREDIT'))
    result=await call(app,'money.state',{})
    assert result.status=='ok'
    assert result.data['currency_id']=='primary'
    assert result.data['display_name']=='Community credits'
    assert result.data['code']=='CREDIT'
    assert result.data['scale']==6
