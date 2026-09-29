"""Credential delivery recovery has one bounded deadline per issuance lineage."""

import os
from dataclasses import replace
from datetime import timedelta

import pytest
from test_service import NOW, temporary_v3_args

from msg.admin.backups import _write_restored_config
from msg.config import load_settings, write_example
from msg.core.codec import b64, parse_time
from msg.core.errors import Failure
from msg.core.requests import request_for


def secret():
    return b64(os.urandom(32))


@pytest.mark.parametrize(
    'configured,seconds',
    [
        (None, 900),
        ('1m', 60),
        ('15m', 900),
        ('60m', 3600),
    ],
)
def test_recovery_window_default_and_bounded_configuration(tmp_path, configured, seconds):
    directory = tmp_path / 'etc'
    write_example(directory, tmp_path / 'data')
    path = directory / 'msgd.toml'
    if configured is None:
        path.write_text(
            path.read_text().replace(
                '[identity]\ncredential_delivery_recovery_window = "15m"\n\n', ''
            )
        )
    else:
        path.write_text(path.read_text().replace('"15m"', f'"{configured}"'))
    assert load_settings(directory).credential_delivery_recovery_window == seconds


@pytest.mark.parametrize(
    'line',
    [
        'credential_delivery_recovery_window = "0m"',
        'credential_delivery_recovery_window = "61m"',
        'credential_delivery_recovery_window = "1h"',
        'credential_delivery_recovery_window = 900',
        'credential_delivery_recovery_window = "999999999999999999999m"',
        'other_identity_setting = true',
    ],
)
def test_recovery_window_rejects_invalid_or_unknown_configuration(tmp_path, line):
    directory = tmp_path / 'etc'
    write_example(directory, tmp_path / 'data')
    path = directory / 'msgd.toml'
    path.write_text(path.read_text().replace('credential_delivery_recovery_window = "15m"', line))
    with pytest.raises(Failure):
        load_settings(directory)


def test_restore_keeps_configured_credential_recovery_window(tmp_path):
    source = tmp_path / 'source'
    write_example(source, tmp_path / 'data')
    config = source / 'msgd.toml'
    config.write_text(config.read_text().replace('"15m"', '"7m"'))
    original = load_settings(source)
    target = tmp_path / 'restored'
    target.mkdir()
    settings = replace(original, server=replace(original.server, config_dir=target))
    _write_restored_config(config, settings, original.server.postgres_dsn)
    assert load_settings(target).credential_delivery_recovery_window == 420


@pytest.mark.asyncio
async def test_recovery_retry_and_successor_keep_original_commit_deadline(installed):
    app, _ = installed
    app.settings = replace(app.settings, credential_delivery_recovery_window=120)
    current = [NOW]
    def clock():
        return current[0]
    app.clock = clock
    app.executor.clock = clock
    app.authenticator.clock = clock

    def packet(operation, args, *, subject=None, request_id=None, version=1):
        return request_for(
            operation,
            args,
            app.settings.service_url,
            subject=subject,
            request_id=request_id,
            contract_version=version,
            expires_at=current[0] + timedelta(seconds=100),
        )

    args, _, _, _ = temporary_v3_args(request_id='commit-anchor')
    original = packet('identity.temporary', args, request_id='commit-anchor', version=3)
    hook = app.executor.response_hook
    app.executor.response_hook = None  # business commit, but no secret released
    try:
        committed = await app.executor.execute(original)
    finally:
        app.executor.response_hook = hook
    assert committed.status == 'ok' and 'token' not in committed.data
    old_id = committed.data['credential_id']
    subject = committed.data['subject_id']

    current[0] = NOW + timedelta(seconds=60)
    claimed = await app.executor.execute(original)
    assert claimed.status == 'ok' and claimed.data['token']
    bad = {
        'credential_id': old_id,
        'original_request_id': 'commit-anchor',
        'recovery_secret': secret(),
        'nonce': secret(),
        'new_recovery_secret': secret(),
    }
    rejected = await app.executor.execute(packet('identity.token_recover', bad, subject=subject))
    assert rejected.error.code == 'recovery_unavailable'

    current[0] = NOW + timedelta(seconds=119)
    first = {**bad, 'recovery_secret': args['recovery_secret']}
    recovered = await app.executor.execute(
        packet('identity.token_recover', first, subject=subject, request_id='first-recovery')
    )
    assert recovered.status == 'ok' and recovered.data['token']
    successor = recovered.data['credential_id']
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one(
            'SELECT recovery_expires_at FROM token_deliveries WHERE credential_id=?', (old_id,)
        )[0]
        after = tx.one(
            'SELECT recovery_expires_at FROM token_deliveries WHERE credential_id=?', (successor,)
        )[0]
        assert parse_time(before) == parse_time(after) == NOW + timedelta(seconds=120)

    current[0] = NOW + timedelta(seconds=120)
    next_args = {
        'credential_id': successor,
        'original_request_id': 'first-recovery',
        'recovery_secret': first['new_recovery_secret'],
        'nonce': secret(),
        'new_recovery_secret': secret(),
    }
    expired = await app.executor.execute(
        packet('identity.token_recover', next_args, subject=subject)
    )
    assert expired.error.code == 'recovery_unavailable'
