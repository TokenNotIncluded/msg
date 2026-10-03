"""A custodian can recover ciphertext offline without gaining account authority."""

from __future__ import annotations

import shutil
import subprocess
import sys
from types import SimpleNamespace

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
    _encode,
    encryption_key_id,
    generate_age_key,
    public_from_recipient,
    recipient_from_identity,
)
from msg.security.age_recipients import recovery_recipient_fingerprint
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app

# Public encryption-only vector from age-plugin-yubikey tests/integration.rs.
YUBIKEY_RECIPIENT = 'age1yubikey1q2w7u3vpya839jxxuq8g0sedh3d740d4xvn639sqhr95ejj8vu3hyfumptt'
# C2SP age v1.1.0 public tagged P-256 vector; age >= 1.3 encrypts it natively.
TAG_RECIPIENT = 'age1tag1qt8lw0ual6avlwmwatk888yqnmdamm7xfd0wak53ut6elz5c4swx2yqdj4e'


@pytest.fixture
def local_encryption_state(tmp_path):
    secret, recipient = generate_age_key()
    path = tmp_path / 'account.agekey'
    path.write_text(secret + '\n')
    path.chmod(0o600)
    return SimpleNamespace(
        subject='u_local_fixture', encryption_recipient=recipient, age_key_path=path
    )


@pytest.mark.parametrize('recipient', ['age1yubikey1opaque', 'AGE-PLUGIN-YUBIKEY-1SECRET'])
def test_invalid_recovery_recipient_fails_before_encryption(
    local_encryption_state, monkeypatch, recipient
):
    from msg import client_recovery as module

    monkeypatch.setattr(module, '_age', lambda *args, **kwargs: pytest.fail('invalid recipient'))
    with pytest.raises(Failure, match='invalid_age_recipient'):
        create_recovery_envelope(local_encryption_state, [recipient])


def test_plugin_recipient_does_not_relax_account_encryption_identity(
    local_encryption_state, monkeypatch
):
    from msg import client_recovery as module

    secret, _ = generate_age_key()
    local_encryption_state.age_key_path.write_text(secret + '\n')
    monkeypatch.setattr(module, '_age', lambda *args, **kwargs: pytest.fail('mismatched identity'))
    with pytest.raises(Failure, match='encryption_identity_mismatch'):
        create_recovery_envelope(local_encryption_state, [_encode('age1tag', b'public')])


@pytest.mark.parametrize('available', [False, True])
def test_real_age_missing_or_failing_plugin_keeps_stderr_private(
    local_encryption_state, tmp_path, monkeypatch, capfd, available
):
    from msg import client_recovery as module

    executable = shutil.which('age')
    if executable is None:
        pytest.skip('real age CLI is required')
    directory = tmp_path / 'plugins'
    directory.mkdir()
    canary = 'PRIVATE_PLUGIN_STDERR_CANARY'
    if available:
        plugin = directory / 'age-plugin-msg-test'
        plugin.write_text(
            f'#!{sys.executable}\nimport sys\nsys.stderr.write({canary!r})\nsys.exit(1)\n'
        )
        plugin.chmod(0o700)
    monkeypatch.setenv('PATH', str(directory))
    monkeypatch.setattr(module.shutil, 'which', lambda name: executable if name == 'age' else None)
    recipient = _encode('age1msg-test', b'public')
    with pytest.raises(Failure, match='age_operation_failed') as failure:
        create_recovery_envelope(local_encryption_state, [recipient])
    assert canary not in str(failure.value) and canary not in repr(failure.value.as_dict())
    assert capfd.readouterr() == ('', '')


@pytest.mark.parametrize('recipient', [YUBIKEY_RECIPIENT, TAG_RECIPIENT])
def test_real_public_recovery_recipient_encryption_and_native_restore(
    local_encryption_state, tmp_path, recipient
):
    executable = shutil.which('age')
    if executable is None:
        pytest.skip('real age CLI is required')
    if recipient == YUBIKEY_RECIPIENT and shutil.which('age-plugin-yubikey') is None:
        pytest.skip('real age-plugin-yubikey is required')
    if recipient == TAG_RECIPIENT:
        version = (
            subprocess
            .run([executable, '--version'], check=True, capture_output=True, text=True)
            .stdout.strip()
            .removeprefix('v')
        )
        if tuple(int(part) for part in version.split('.')[:2]) < (1, 3):
            if shutil.which('age-plugin-tag') is None:
                pytest.skip('age >= 1.3 or age-plugin-tag is required')
    identity, native = generate_age_key()
    path = tmp_path / 'recovery.agekey'
    path.write_text(identity + '\n')
    path.chmod(0o600)
    plugin_only, metadata = create_recovery_envelope(local_encryption_state, [recipient])
    assert metadata['recipient_fingerprints'] == (recovery_recipient_fingerprint(recipient),)
    assert local_encryption_state.age_key_path.read_bytes().strip() not in plugin_only
    ciphertext, metadata = create_recovery_envelope(local_encryption_state, [recipient, native])
    restored = tmp_path / 'restored.agekey'
    result = restore_recovery_envelope(
        ciphertext,
        path,
        restored,
        expected_subject_id=local_encryption_state.subject,
        expected_encryption_key_id=metadata['encryption_key_id'],
    )
    assert result['account_authority'] == 'none'
    assert restored.read_bytes() == local_encryption_state.age_key_path.read_bytes()


@pytest.mark.asyncio
async def test_real_plugin_recovery_envelope_matches_policy_and_native_restore(installed, tmp_path):
    if shutil.which('age') is None or shutil.which('age-plugin-yubikey') is None:
        pytest.skip('real age and age-plugin-yubikey are required')
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        state = ClientState(tmp_path / 'client', server=app.settings.service_url)
        client = MsgClient(
            state, HTTPTransport(app.settings.service_url, http=http), clock=lambda: NOW
        )
        await client.register('plugin-envelope-owner')
        identity, native = generate_age_key()
        path = tmp_path / 'native.agekey'
        path.write_text(identity + '\n')
        path.chmod(0o600)
        recipients = (native, YUBIKEY_RECIPIENT)
        own_key = encryption_key_id(public_from_recipient(state.encryption_recipient))
        policy = client.checked(
            await client.call(
                'identity.recovery_policy_set',
                {
                    'expected_version': 0,
                    'encryption_key_id': own_key,
                    'recipients': [{'recipient': recipient} for recipient in recipients],
                },
                contract_version=2,
            )
        )
        expected = {recovery_recipient_fingerprint(recipient) for recipient in recipients}
        assert {entry['fingerprint'] for entry in policy.data['recipients']} == expected
        stored, registered, metadata = await save_recovery_envelope(
            client, recipients, policy_version=1
        )
        assert set(metadata['recipient_fingerprints']) == expected
        assert set(registered.data['recipient_fingerprints']) == expected
        ciphertext = tmp_path / 'plugin-envelope.age'
        await client.download(
            client.checked(
                await client.call('keystore.get', {'id': stored.resources[0].id})
            ).output,
            ciphertext,
        )
        assert state.age_key_path.read_bytes().strip() not in ciphertext.read_bytes()
        restored = tmp_path / 'restored.agekey'
        result = restore_recovery_envelope(
            ciphertext.read_bytes(),
            path,
            restored,
            expected_subject_id=state.subject,
            expected_encryption_key_id=own_key,
        )
        assert result['account_authority'] == 'none'
        assert restored.read_bytes() == state.age_key_path.read_bytes()
        with pytest.raises(Failure, match='recovery_recipient_not_in_policy'):
            await save_recovery_envelope(client, [generate_age_key()[1]], policy_version=1)


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
