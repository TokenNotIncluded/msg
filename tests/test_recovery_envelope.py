"""A custodian can recover ciphertext offline without gaining account authority."""

from __future__ import annotations

import shutil

import httpx
import pytest
from test_service import NOW

from msg.client import ClientState, MsgClient
from msg.client_recovery import (
    _age,
    create_recovery_envelope,
    restore_recovery_envelope,
    rewrap_age_keystore_entry,
    save_recovery_envelope,
)
from msg.core.codec import b64
from msg.core.errors import Failure
from msg.security.age_keys import (
    encryption_key_id,
    generate_age_key,
    public_from_recipient,
    recipient_from_identity,
)
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_age_multi_recipient_backup_is_opt_in_and_offline(installed, tmp_path):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        state = ClientState(tmp_path / 'client', server=app.settings.service_url)
        client = MsgClient(
            state, HTTPTransport(app.settings.service_url, http=http), clock=lambda: NOW
        )
        await client.register('recovery-owner')
        assert not (await client.call('keystore.list', {})).data['items']
        first_secret, first = generate_age_key()
        second_secret, second = generate_age_key()
        third_secret, third = generate_age_key()
        for name, secret in (
            ('first', first_secret),
            ('second', second_secret),
            ('third', third_secret),
        ):
            path = tmp_path / (name + '.agekey')
            path.write_text(secret + '\n')
            path.chmod(0o600)
        with pytest.raises(Failure, match='independent_recovery_recipient_required'):
            create_recovery_envelope(state, (state.encryption_recipient,))
        key_id = encryption_key_id(public_from_recipient(state.encryption_recipient))
        policy = client.checked(
            await client.call(
                'identity.recovery_policy_set',
                {
                    'expected_version': 0,
                    'encryption_key_id': key_id,
                    'recipients': [{'recipient': first}, {'recipient': second}],
                },
            )
        )
        assert policy.data['version'] == 1 and policy.data['opted_in']
        result, registered, metadata = await save_recovery_envelope(
            client, (first, second), policy_version=policy.data['version']
        )
        assert result.status == registered.status == 'ok'
        assert registered.data['recipient_claim'] == 'owner_declared_unverified'
        assert metadata['subject_id'] == state.subject
        assert metadata['encryption_key_id'] == encryption_key_id(
            public_from_recipient(state.encryption_recipient)
        )
        assert len(metadata['recipient_fingerprints']) == 2
        entry = client.checked(await client.call('keystore.get', {'id': result.resources[0].id}))
        assert entry.data['format'] == 'age'
        ciphertext = tmp_path / 'download.age'
        await client.download(entry.output, ciphertext)
        for name in ('first', 'second'):
            restored = tmp_path / (name + '-restored.agekey')
            proof = restore_recovery_envelope(
                ciphertext.read_bytes(),
                tmp_path / (name + '.agekey'),
                restored,
                expected_subject_id=state.subject,
                expected_encryption_key_id=metadata['encryption_key_id'],
            )
            assert proof['account_authority'] == 'none'
            assert (
                recipient_from_identity(restored.read_text().strip()) == state.encryption_recipient
            )
            assert restored.stat().st_mode & 0o077 == 0
        with pytest.raises(Failure, match='age_operation_failed'):
            restore_recovery_envelope(
                ciphertext.read_bytes(),
                tmp_path / 'third.agekey',
                tmp_path / 'third-restored.agekey',
                expected_subject_id=state.subject,
                expected_encryption_key_id=metadata['encryption_key_id'],
            )
        with pytest.raises(Failure, match='recovery_envelope_mismatch'):
            restore_recovery_envelope(
                ciphertext.read_bytes(),
                tmp_path / 'first.agekey',
                tmp_path / 'wrong-subject.agekey',
                expected_subject_id='u_wrong',
                expected_encryption_key_id=metadata['encryption_key_id'],
            )
        assert not (tmp_path / 'wrong-subject.agekey').exists()
        assert state.age_key_path.read_text().strip().encode() not in ciphertext.read_bytes()
        assert third != first and third != second


@pytest.mark.asyncio
async def test_selected_age_keystore_rewrap_keeps_old_revision_and_key(installed, tmp_path):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        state = ClientState(tmp_path / 'client', server=app.settings.service_url)
        client = MsgClient(
            state, HTTPTransport(app.settings.service_url, http=http), clock=lambda: NOW
        )
        await client.register('rewrap-owner')
        old_recipient = state.encryption_recipient
        plaintext = b'old ciphertext remains verifiable\n'
        old_ciphertext = _age('--encrypt', '--recipient', old_recipient, input_data=plaintext)
        created = client.checked(
            await client.call(
                'keystore.put',
                {'name': 'rotating-secret', 'format': 'age', 'ciphertext': b64(old_ciphertext)},
            )
        )
        rid, old_revision = created.resources[0].id, created.resources[0].revision
        rotated = client.checked(await client.rotate_encryption_key())
        old_identity = state.directory / (
            'encryption-' + rotated.data['previous_key_id'] + '.agekey'
        )
        assert old_identity.is_file()
        renewed = await rewrap_age_keystore_entry(
            client, rid, old_identity, expected_revision=old_revision
        )
        assert renewed.resources[0].revision != old_revision
        assert old_identity.is_file()
        with pytest.raises(Failure, match='revision_conflict'):
            await rewrap_age_keystore_entry(
                client, rid, old_identity, expected_revision=old_revision
            )
        for revision, identity_path in (
            (old_revision, old_identity),
            (renewed.resources[0].revision, state.age_key_path),
        ):
            entry = client.checked(
                await client.call('keystore.get', {'id': rid, 'revision': revision})
            )
            ciphertext = tmp_path / (revision + '.age')
            await client.download(entry.output, ciphertext)
            assert (
                _age(
                    '--decrypt',
                    '--identity',
                    str(identity_path),
                    input_data=ciphertext.read_bytes(),
                )
                == plaintext
            )
