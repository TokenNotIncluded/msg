import pytest
from msg.admin.rotation import prepare,complete,journal_path
from msg.security.crypto import Ed25519Signer,open_private_key
from msg.core.codec import loads,wire
from msg.application import Application
from msg.admin.root import _approve_csr,root_envelope
from test_service import call,register,NOW

@pytest.mark.asyncio
async def test_root_rotation_retires_old_chain_and_requires_explicit_online_ca_approval(installed):
    app,root=installed
    key,uid,cert=await register(app,'rotation-agent')
    next_key=Ed25519Signer.generate()
    pin='new-root-long-passphrase'
    journal=prepare(app,next_key,pin,old_signer=root,operator='isolated-test-console')
    assert journal['new_trust']['certificate']['issuance']['max_child_ca_depth']==3
    assert journal['online_csr']['issuance']['max_child_ca_depth']==0
    assert journal_path(app).exists()
    assert next_key.private_bytes() not in journal_path(app).read_bytes()
    outcome=await complete(app,journal,pin=pin)
    assert outcome['restart_required'] and not journal_path(app).exists()
    stale=await call(app,'discovery.get',{'id':'/main'})
    assert stale.error.code=='service_restart_required'
    fresh=Application(app.settings,clock=lambda:NOW);await fresh.load()
    try:
        denied=await call(fresh,'discovery.get',{'id':'/main'},key=key,subject=uid,certs=(cert,))
        assert denied.error.code=='certificate_revoked'
        renewal=await call(fresh,'identity.certificate_renew',{},key=key,subject=uid)
        assert renewal.error.code=='issuer_not_ready'
        await _approve_csr(fresh,outcome['online_ca_request'],next_key,expected_digest=None,operator='isolated-test-console')
        renewal=await call(fresh,'identity.certificate_renew',{},key=key,subject=uid)
        assert renewal.error.code=='renewal_source_required',wire(renewal)
        assert fresh.certificates.root_public_key==next_key.public_key
        assert not (app.settings.config_dir/'root').exists()
        envelope=loads(root_envelope(app.settings.config_dir).read_bytes())
        assert open_private_key(envelope,pin)==next_key.private_bytes()
    finally:await fresh.close()
