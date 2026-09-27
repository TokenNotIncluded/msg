"""System ledger accounts cannot be promoted into network Subjects."""
import pytest

from msg.admin.money import apply_money
from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.models import Scope
from msg.security.age_keys import generate_age_key
from msg.security.capabilities import grant_for
from msg.security.crypto import Ed25519Signer
from test_ledger_accounts import _market_accounts
from test_service import call


@pytest.mark.asyncio
async def test_ledger_account_cannot_be_registered_recovered_delegated_or_used_as_bank(installed):
    app, root = installed
    owner_key, owner, _, _, bounty, order = await _market_accounts(app, root)
    accounts = (bounty.data['funding']['body']['to_subject'],
                order.data['payment']['body']['to_subject'])

    async with app.metadata.transaction(write=False) as tx:
        for account, kind in zip(accounts, ('bounty_escrow', 'order_escrow')):
            assert tx.one('SELECT kind,subject_id FROM ledger_accounts WHERE id=?',
                          (account,)) == (kind, None)
            assert tx.one('SELECT 1 FROM identities WHERE id=?', (account,)) is None

    # A registration request claiming the ledger ID must not create an identity,
    # even when it carries a valid signature from an unrelated signing key.
    signer = Ed25519Signer.generate()
    _, recipient = generate_age_key()
    registration = await call(app, 'identity.register', {
        'handle': 'ledger-account', 'public_key': b64(signer.public_key),
        'encryption_recipient': recipient}, key=signer, subject=accounts[0],
        contract_version=2)
    assert registration.status == 'error', wire(registration)

    recovery_key = Ed25519Signer.generate()
    proof = recovery_key.sign(canonical({'subject_id': accounts[0],
                                         'public_key': b64(recovery_key.public_key)}),
                              purpose='recover')
    recovered = await call(app, 'identity.recover', {
        'subject': accounts[0], 'public_key': b64(recovery_key.public_key),
        'possession_proof': wire(proof)}, key=owner_key, subject=owner)
    assert recovered.status == 'error', wire(recovered)

    key = await call(app, 'identity.delegate', {
        'grantee': accounts[0], 'key_id': owner_key.key_id,
        'grants': wire((grant_for(app.registry.capability('discovery.basic'),
                                  scope=Scope(resource_id='t_main'),
                                  operations=('discovery.get@1',)),)),
        'ttl': 600}, key=owner_key, subject=owner)
    assert key.status == 'error', wire(key)

    with pytest.raises(Failure) as bank_role:
        await apply_money(app, root, action='bank_add', operator='test-console',
                          subject_id=accounts[0])
    assert bank_role.value.code in {'subject_not_found', 'invalid_money_recipient'}

    incoming = await call(app, 'money.transfer', {
        'to_subject': accounts[0], 'currency_id': 'primary', 'amount_minor': 1},
        key=owner_key, subject=owner)
    outgoing = await call(app, 'money.transfer', {
        'from_subject': accounts[0], 'to_subject': owner,
        'currency_id': 'primary', 'amount_minor': 1}, key=owner_key, subject=owner)
    assert incoming.status == outgoing.status == 'error'

    async with app.metadata.transaction(write=False) as tx:
        for account in accounts:
            assert tx.one('SELECT 1 FROM identities WHERE id=?', (account,)) is None
            for table in ('credentials', 'identity_keys', 'certificates', 'money_bank_roles'):
                assert tx.one(f'SELECT COUNT(*) FROM {table} WHERE subject=?' if table != 'money_bank_roles'
                              else 'SELECT COUNT(*) FROM money_bank_roles WHERE subject_id=?',
                              (account,))[0] == 0
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 5
