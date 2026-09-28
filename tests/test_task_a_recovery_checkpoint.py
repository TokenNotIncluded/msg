"""Independent checkpoint pins, not database flags, authorize deny-only replay."""
import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from msg.admin.recovery_replay import TrustedCheckpointPin, replay, verify_checkpoint
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import Failure
from msg.core.models import Credential
from msg.security.crypto import Ed25519Signer
from msg.security.quarantine import SETTING, active
from msg.storage.postgres import PostgresMetadataStore
from test_service import NOW


@pytest.fixture
def checkpoint():
    signer = Ed25519Signer.generate()
    body = {'format': 'msg-revocation-checkpoint-v1', 'service': 'http://testserver',
            'source_backup_sha256': 'a' * 64, 'sequence': 1,
            'entries': [{'sequence': 1, 'kind': 'credential.revoke', 'subject': 'u_owner',
                         'target': 'token_one', 'at': wire(NOW)}]}
    pin = TrustedCheckpointPin(service=body['service'], public_key=signer.public_key,
                               digest=digest(body), sequence=1)
    def signed(value):
        return {'checkpoint': value,
                'signature': wire(signer.sign(canonical(value), purpose='recovery-checkpoint-v1'))}
    return body, pin, signed


@pytest.fixture
async def restored(pg_dsn):
    store = PostgresMetadataStore(pg_dsn)
    token = Credential(id='token_one', subject_id='u_owner', kind='token', verifier=b'x' * 32,
                       ceiling=(), not_before=NOW, expires_at=None, revoked_at=None)
    async with store.transaction(write=True) as tx:
        tx.set_setting(SETTING, {'format': 'msg-recovery-quarantine-v1',
                                'source_backup_sha256': 'a' * 64,
                                'outbound_enabled': False, 'authority': 'health_only',
                                'revocation_replay': 'required'})
        tx.execute('INSERT INTO credentials VALUES (?,?,?)',
                   (token.id, token.subject_id, canonical(token).decode()), write=True)
    yield store
    await store.close()


def test_checkpoint_requires_an_independently_supplied_exact_pin(checkpoint):
    body, pin, signed = checkpoint
    assert verify_checkpoint(signed(body), pin=pin) == body
    wrong = replace(pin, public_key=Ed25519Signer.generate().public_key)
    with pytest.raises(Failure, match='^recovery_checkpoint_invalid$'):
        verify_checkpoint(signed(body), pin=wrong)
    with pytest.raises(Failure, match='^recovery_checkpoint_pin_required$'):
        verify_checkpoint(signed(body), pin=None)


@pytest.mark.parametrize('change', ['gap', 'rollback', 'unknown', 'extra', 'service', 'bool'])
def test_resigned_but_untrusted_or_malformed_checkpoint_denies(checkpoint, change):
    body, pin, signed = checkpoint
    bad = deepcopy(body)
    if change == 'gap':
        bad['entries'][0]['sequence'] = 2
    elif change == 'rollback':
        bad.update(sequence=0, entries=[])
    elif change == 'unknown':
        bad['entries'][0]['kind'] = 'credential.grant'
    elif change == 'extra':
        bad['entries'][0]['private_key'] = 'never accepted'
    elif change == 'service':
        bad['service'] = 'https://another.example'
    else:
        bad['sequence'] = True
    # Even an authentic older signature cannot replace an independently pinned head.
    with pytest.raises(Failure, match='^recovery_checkpoint_invalid$'):
        verify_checkpoint(signed(bad), pin=pin)
    if change in {'gap', 'unknown', 'extra', 'bool'}:
        with pytest.raises(Failure, match='^recovery_checkpoint_invalid$'):
            verify_checkpoint(signed(bad), pin=replace(pin, digest=digest(bad)))


@pytest.mark.asyncio
async def test_replay_is_atomic_monotonic_idempotent_and_never_promotes(restored, checkpoint):
    body, pin, signed = checkpoint
    results = await asyncio.gather(*(replay(restored, signed(body), pin=pin) for _ in range(3)))
    assert sum(result['changed'] for result in results) == 1
    assert all(result['promotion'] == 'blocked' and result['backup_retired'] is False for result in results)
    async with restored.transaction(write=False) as tx:
        assert active(tx)
        credential = await tx.credential('token_one')
        assert credential.revoked_at == NOW and credential.verifier == b'x' * 32
        assert tx.one('SELECT COUNT(*) FROM audit')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM jobs')[0] == 0
        assert tx.setting('authorization_epoch') == 1
    restarted = PostgresMetadataStore(restored.dsn, initialize=False)
    try:
        assert (await replay(restarted, signed(body), pin=pin))['changed'] == 0
        async with restarted.transaction(write=False) as tx:
            assert active(tx)
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_missing_or_wrong_subject_fact_rolls_back_earlier_revocation(restored, checkpoint):
    body, pin, signed = checkpoint
    body = deepcopy(body)
    body['entries'].append({'sequence': 2, 'kind': 'credential.revoke',
                            'subject': 'u_owner', 'target': 'missing', 'at': wire(NOW)})
    body['sequence'] = 2
    pin = replace(pin, digest=digest(body), sequence=2)
    with pytest.raises(Failure, match='^recovery_fact_missing$'):
        await replay(restored, signed(body), pin=pin)
    async with restored.transaction(write=False) as tx:
        assert (await tx.credential('token_one')).revoked_at is None
        assert active(tx) and tx.setting('recovery_replay') is None
        assert tx.one('SELECT COUNT(*) FROM audit')[0] == 0
    body['entries'][1].update(target='token_one', subject='u_other')
    with pytest.raises(Failure, match='^recovery_fact_subject_mismatch$'):
        await replay(restored, signed(body), pin=replace(pin, digest=digest(body)))
    async with restored.transaction(write=False) as tx:
        assert (await tx.credential('token_one')).revoked_at is None


@pytest.mark.asyncio
async def test_wrong_backup_and_live_database_are_not_replay_targets(restored, checkpoint):
    body, pin, signed = checkpoint
    async with restored.transaction(write=True) as tx:
        gate = tx.setting(SETTING)
        tx.set_setting(SETTING, dict(gate, source_backup_sha256='b' * 64))
    with pytest.raises(Failure, match='^recovery_checkpoint_backup_mismatch$'):
        await replay(restored, signed(body), pin=pin)
    async with restored.transaction(write=True) as tx:
        tx.execute('DELETE FROM settings WHERE key=?', (SETTING,), write=True)
    with pytest.raises(Failure, match='^recovery_quarantine_required$'):
        await replay(restored, signed(body), pin=pin)
