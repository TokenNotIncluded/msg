"""Real age round trips restore one identity into a fresh environment without writes online."""

import os
import shutil
import sys
import time

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.atomic_file import durable_write
from msg.client import ClientState, MsgClient
from msg.client_account_backup import backup_account, restore_account
from msg.core.codec import b64, canonical, digest, loads
from msg.core.errors import Failure
from msg.paths import ClientPaths
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer, subject_id, verify
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app

SERVER = 'https://example.org'


def environment(monkeypatch, directory):
    for scope in ('CONFIG', 'DATA', 'STATE', 'CACHE'):
        monkeypatch.setenv(f'XDG_{scope}_HOME', str(directory / scope.lower()))
    monkeypatch.delenv('MSG_SERVER', raising=False)


@pytest.fixture
def local(monkeypatch, tmp_path):
    if shutil.which('age') is None:
        pytest.skip('real age CLI is required')
    environment(monkeypatch, tmp_path / 'before')
    state = ClientState(server=SERVER, account='original')
    signer = Ed25519Signer.generate()
    state.save_signer(signer)
    state.ensure_encryption_key()
    state.data.update(
        subject_id=subject_id(signer.public_key), certificates=['c_example'], handle='alice'
    )
    state._save()
    identity, recipient = generate_age_key()
    recovery = tmp_path / 'recovery.agekey'
    durable_write(recovery, (identity + '\n').encode(), mode=0o600)
    return state, recovery, recipient


def save(local, tmp_path):
    state, _, recipient = local
    output = tmp_path / 'identity.age'
    receipt = backup_account(SERVER, 'original', [recipient], output)
    return output, receipt


async def invoke(capsys, *arguments):
    parsed = cli.parser().parse_args(list(arguments))
    assert await cli.run(parsed) == 0
    return loads(capsys.readouterr().out.strip())


async def test_real_cli_fresh_environment_preserves_identity_and_no_default_switch(
    local, tmp_path, monkeypatch, capsys
):
    original, recovery, recipient = local
    nested = original.paths.state / 'agent-listeners'
    nested.mkdir(mode=0o700)
    durable_write(nested / 'cursor.json', canonical({'cursor': 'resume-here'}))
    durable_write(original.paths.config / 'policy.json', b'private-policy')
    durable_write(original.paths.data / 'historical.agekey', b'historical-encryption-key')
    durable_write(original.paths.cache / 'do-not-back-up', b'cache')
    output = tmp_path / 'identity.age'
    before_files = {
        p: p.read_bytes()
        for root in (original.paths.data, original.paths.state, original.paths.config)
        for p in root.rglob('*')
        if p.is_file()
    }
    receipt = await invoke(
        capsys,
        '--server',
        SERVER,
        '--account',
        'original',
        'account',
        'backup',
        '--recipient',
        recipient,
        '--output',
        str(output),
    )
    assert receipt['subject_id'] == original.subject and receipt['key_id'] == original.signer.key_id
    assert receipt['ciphertext_sha256'] == digest(output.read_bytes())
    assert output.stat().st_mode & 0o777 == 0o600
    assert original.signer.private_bytes() not in output.read_bytes()
    assert before_files == {p: p.read_bytes() for p in before_files}
    environment(monkeypatch, tmp_path / 'after')
    restored = await invoke(
        capsys,
        '--server',
        SERVER,
        'account',
        'restore',
        'recovered',
        '--input',
        str(output),
        '--identity',
        str(recovery),
        '--expected-subject',
        original.subject,
        '--expected-key-id',
        original.signer.key_id,
        '--expected-sha256',
        receipt['ciphertext_sha256'],
    )
    assert restored['selected'] is False and restored['server_credentials_verified'] is False
    assert restored['source_account'] == 'original'
    assert not (ClientPaths.discover(server=SERVER).config / 'current-account.json').exists()
    paths = ClientPaths.discover(server=SERVER, account='recovered')
    assert not paths.cache.exists()
    for root in (paths.config, paths.data, paths.state):
        assert root.stat().st_mode & 0o777 == 0o700
        for p in root.rglob('*'):
            assert p.stat().st_mode & 0o777 == (0o700 if p.is_dir() else 0o600)
    current = ClientState(server=SERVER, account='recovered')
    assert current.subject == original.subject
    assert current.signer.key_id == original.signer.key_id
    assert current.encryption_recipient == original.encryption_recipient
    assert current.certificates == original.certificates
    assert (paths.state / 'agent-listeners/cursor.json').read_bytes() == (
        nested / 'cursor.json'
    ).read_bytes()
    assert (paths.config / 'policy.json').read_bytes() == b'private-policy'
    proof = current.signer.sign(b'restored-key', purpose='operation')
    verify(original.signer.public_key, b'restored-key', proof, purpose='operation')
    assert current.signer.private_bytes() == original.signer.private_bytes()


async def test_restored_account_retains_real_signed_server_authority(
    installed, tmp_path, monkeypatch
):
    if shutil.which('age') is None:
        pytest.skip('real age CLI is required')
    app, _ = installed
    server = app.settings.service_url
    environment(monkeypatch, tmp_path / 'old')
    state = ClientState(server=server, account='temporary-fixture')
    identity, recipient = generate_age_key()
    recovery = tmp_path / 'offline-key.agekey'
    durable_write(recovery, (identity + '\n').encode())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=server
    ) as http:
        client = MsgClient(state, HTTPTransport(server, http=http), clock=lambda: NOW)
        registered = await client.register('backup-roundtrip-fixture')
        assert registered.status == 'ok'
        ciphertext = tmp_path / 'fixture.age'
        backup_account(server, state.account, [recipient], ciphertext)
        environment(monkeypatch, tmp_path / 'new')
        restore_account(
            server, 'restored-fixture', ciphertext, [recovery], expected_subject=state.subject
        )
        restored = ClientState(server=server, account='restored-fixture')
        restored_client = MsgClient(restored, HTTPTransport(server, http=http), clock=lambda: NOW)
        result = await restored_client.call('discovery.get', {'id': state.subject, 'view': 'meta'})
        assert result.status == 'ok' and result.actor == state.subject
        assert restored.signer.key_id == registered.data['key_id']


@pytest.mark.parametrize(
    'failure', ['wrong-key', 'tampered', 'subject', 'server', 'key-id', 'sha256']
)
def test_invalid_restore_never_creates_destination(local, tmp_path, monkeypatch, failure):
    state, recovery, _ = local
    ciphertext, _ = save(local, tmp_path)
    server, subject, key, sha = SERVER, state.subject, state.signer.key_id, None
    if failure == 'wrong-key':
        wrong, _ = generate_age_key()
        recovery = tmp_path / 'wrong.agekey'
        durable_write(recovery, (wrong + '\n').encode())
    elif failure == 'tampered':
        raw = ciphertext.read_bytes()
        durable_write(ciphertext, raw[:-1] + bytes([raw[-1] ^ 1]))
    elif failure == 'subject':
        subject = 'u_' + '0' * 32
    elif failure == 'server':
        server = 'https://different.example'
    elif failure == 'key-id':
        key = 'k_' + '0' * 64
    else:
        sha = 'sha256:' + '0' * 64
    environment(monkeypatch, tmp_path / 'after')
    with pytest.raises(Failure):
        restore_account(
            server,
            'recovered',
            ciphertext,
            [recovery],
            expected_subject=subject,
            expected_key_id=key,
            expected_sha256=sha,
        )
    paths = ClientPaths.discover(server=server, account='recovered')
    assert not any(p.exists() for p in (paths.config, paths.data, paths.state, paths.cache))


def test_existing_outputs_accounts_and_default_selection_are_preserved(local, tmp_path):
    state, recovery, recipient = local
    ciphertext, _ = save(local, tmp_path)
    original = ciphertext.read_bytes()
    with pytest.raises(Failure, match='account_backup_destination_exists'):
        backup_account(SERVER, state.account, [recipient], ciphertext)
    assert ciphertext.read_bytes() == original
    before = state.key_path.read_bytes()
    with pytest.raises(Failure, match='account_restore_destination_exists'):
        restore_account(
            SERVER, state.account, ciphertext, [recovery], expected_subject=state.subject
        )
    assert state.key_path.read_bytes() == before
    selected = ClientPaths.discover(server=SERVER).config / 'current-account.json'
    assert not selected.exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'unsafe-file', 'unsafe-directory', 'fifo'])
def test_unsafe_source_never_emits_ciphertext(local, tmp_path, kind):
    state, _, recipient = local
    target = state.paths.state / 'unsafe'
    if kind == 'symlink':
        target.symlink_to(state.key_path)
    elif kind == 'hardlink':
        os.link(state.key_path, target)
    elif kind == 'unsafe-file':
        target.write_bytes(b'secret')
        target.chmod(0o644)
    elif kind == 'unsafe-directory':
        target.mkdir(mode=0o755)
        target.chmod(0o755)
    else:
        os.mkfifo(target, 0o600)
    output = tmp_path / 'must-not-exist.age'
    with pytest.raises(Failure):
        backup_account(SERVER, state.account, [recipient], output)
    assert not output.exists()


def test_source_change_during_encryption_is_rejected(local, tmp_path, monkeypatch):
    from msg import client_account_backup as module

    state, _, recipient = local
    real_age = module._age

    def changed(*args, **kwargs):
        ciphertext = real_age(*args, **kwargs)
        durable_write(state.paths.state / 'new-change.json', canonical({'changed': True}))
        return ciphertext

    monkeypatch.setattr(module, '_age', changed)
    output = tmp_path / 'must-not-exist.age'
    with pytest.raises(Failure, match='account_backup_changed'):
        backup_account(SERVER, state.account, [recipient], output)
    assert not output.exists()


@pytest.mark.parametrize(
    'path',
    ['state/../../escape', '/tmp/escape', 'state\\escape', 'state/cache/secret', 'state/./escape'],
)
def test_authenticated_malicious_archive_path_is_rejected(local, tmp_path, monkeypatch, path):
    from msg import client_account_backup as module

    state, recovery, recipient = local
    ciphertext, _ = save(local, tmp_path)
    plaintext = module._age(['--decrypt', '--identity', str(recovery)], ciphertext.read_bytes())
    payload = loads(plaintext)
    payload['files'].append({'path': path, 'data': b64(b'escape'), 'sha256': digest(b'escape')})
    durable_write(
        ciphertext, module._age(['--encrypt', '--recipient', recipient], canonical(payload))
    )
    environment(monkeypatch, tmp_path / 'after')
    with pytest.raises(Failure, match='unsafe_account_backup_path'):
        restore_account(SERVER, 'recovered', ciphertext, [recovery], expected_subject=state.subject)
    assert not ClientPaths.discover(server=SERVER, account='recovered').state.exists()
    assert not (tmp_path / 'escape').exists()


def test_partial_install_failure_removes_only_new_account(local, tmp_path, monkeypatch):
    from msg import client_account_backup as module

    state, recovery, _ = local
    ciphertext, _ = save(local, tmp_path)
    environment(monkeypatch, tmp_path / 'after')
    existing = ClientState(server=SERVER, account='untouched')
    marker = existing.path.read_bytes()
    real_write = module._write_new
    count = 0

    def broken(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise Failure('injected_install_failure')
        return real_write(*args)

    monkeypatch.setattr(module, '_write_new', broken)
    with pytest.raises(Failure, match='injected_install_failure'):
        restore_account(SERVER, 'recovered', ciphertext, [recovery], expected_subject=state.subject)
    paths = ClientPaths.discover(server=SERVER, account='recovered')
    assert not any(p.exists() for p in (paths.config, paths.data, paths.state))
    assert existing.path.read_bytes() == marker


def test_new_directory_open_failure_cleans_account_root(local, tmp_path, monkeypatch):
    from msg import client_account_backup as module

    state, recovery, _ = local
    ciphertext, _ = save(local, tmp_path)
    environment(monkeypatch, tmp_path / 'after')
    real_open = module.os.open

    def fail_new_root(name, flags, *args, **kwargs):
        if name == 'recovered' and kwargs.get('dir_fd') is not None:
            raise OSError('injected directory open failure')
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, 'open', fail_new_root)
    with pytest.raises(OSError, match='injected directory open failure'):
        restore_account(SERVER, 'recovered', ciphertext, [recovery], expected_subject=state.subject)
    paths = ClientPaths.discover(server=SERVER, account='recovered')
    assert not any(p.exists() for p in (paths.config, paths.data, paths.state))


def test_yubikey_descriptor_is_backed_up_without_exporting_or_touching_hardware(
    local, tmp_path, monkeypatch
):
    from msg.client_yubikey import YubiKeySigner

    state, recovery, recipient = local
    descriptor = YubiKeySigner(state.signer.public_key, '82')
    state.key_path.unlink()
    durable_write(state.hardware_path, canonical(descriptor.descriptor()))
    monkeypatch.setattr(YubiKeySigner, 'private_bytes', lambda self: pytest.fail('no export'))
    monkeypatch.setattr(
        YubiKeySigner, 'sign', lambda *args, **kwargs: pytest.fail('no hardware operation')
    )
    output = tmp_path / 'hardware.age'
    receipt = backup_account(SERVER, state.account, [recipient], output)
    assert receipt['signer_backend'] == 'yubikey-piv'
    restore_account(SERVER, 'hardware-restored', output, [recovery], expected_subject=state.subject)
    paths = ClientPaths.discover(server=SERVER, account='hardware-restored')
    assert not paths.file('identity.key').exists()
    assert loads(paths.file('hardware-signer.json').read_bytes()) == descriptor.descriptor()


def test_opaque_plugin_recipient_is_passed_to_age_not_parsed_as_x25519(
    local, tmp_path, monkeypatch
):
    from msg import client_account_backup as module

    state, _, _ = local
    seen = []

    def plugin(arguments, data, **kwargs):
        seen.extend(arguments)
        return b'age-encryption.org/v1\nopaque-test'

    monkeypatch.setattr(module, '_age', plugin)
    backup_account(SERVER, state.account, ['age1yubikey1opaque'], tmp_path / 'opaque.age')
    assert seen == ['--encrypt', '--recipient', 'age1yubikey1opaque']


def test_real_age_ascii_armor_is_accepted(local, tmp_path):
    from msg import client_account_backup as module

    state, recovery, recipient = local
    ciphertext, _ = save(local, tmp_path)
    plaintext = module._age(['--decrypt', '--identity', str(recovery)], ciphertext.read_bytes())
    armored = module._age(['--encrypt', '--armor', '--recipient', recipient], plaintext)
    assert armored.startswith(b'-----BEGIN AGE ENCRYPTED FILE-----\n')
    durable_write(ciphertext, armored)
    restore_account(SERVER, 'from-armor', ciphertext, [recovery], expected_subject=state.subject)
    assert ClientState(server=SERVER, account='from-armor').signer.key_id == state.signer.key_id


async def test_backup_external_signer_and_missing_output_are_explicit_errors(local, tmp_path):
    state, _, recipient = local
    for flags, expected in [
        ([], 'account_backup_output_required'),
        (['--key', str(state.key_path)], 'account_backup_external_signer_not_supported'),
    ]:
        parsed = cli.parser().parse_args([
            '--server',
            SERVER,
            '--account',
            state.account,
            *flags,
            'account',
            'backup',
            '--recipient',
            recipient,
        ])
        with pytest.raises(Failure, match=expected):
            await cli.run(parsed)


@pytest.mark.parametrize('channel', ['stdout', 'stderr', 'timeout'])
def test_age_subprocess_output_caps_and_timeout_kill_and_reap(tmp_path, monkeypatch, channel):
    from msg import client_account_backup as module

    executable = tmp_path / 'fake-age'
    command = (
        'import time; time.sleep(60)'
        if channel == 'timeout'
        else f'import os; os.write({1 if channel == "stdout" else 2}, b"x" * 65536)'
    )
    executable.write_text(f'#!{sys.executable}\n{command}\n')
    executable.chmod(0o700)
    monkeypatch.setattr(module.shutil, 'which', lambda name: str(executable))
    monkeypatch.setattr(module, 'MAX_ENVELOPE', 1024)
    monkeypatch.setattr(module, 'MAX_AGE_STDERR', 1024)
    monkeypatch.setattr(module, 'AGE_TIMEOUT', 0.2 if channel == 'timeout' else 5)
    spawned = []
    real_popen = module.subprocess.Popen

    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    started = time.monotonic()
    with pytest.raises(Failure) as raised:
        module._age(['--encrypt'], b'not-a-real-key')
    assert (
        raised.value.code
        == {
            'stdout': 'account_backup_too_large',
            'stderr': 'age_output_limit_exceeded',
            'timeout': 'age_operation_failed',
        }[channel]
    )
    assert time.monotonic() - started < 3
    assert len(spawned) == 1 and spawned[0].returncode is not None


@pytest.mark.parametrize(
    'flags,code',
    [
        ([], 'account_backup_server_required'),
        (['--server', SERVER], 'account_backup_account_required'),
    ],
)
async def test_cli_requires_explicit_server_and_named_backup_account(local, tmp_path, flags, code):
    _, _, recipient = local
    parsed = cli.parser().parse_args([
        *flags,
        'account',
        'backup',
        '--recipient',
        recipient,
        '--output',
        str(tmp_path / 'no.age'),
    ])
    with pytest.raises(Failure, match=code):
        await cli.run(parsed)
    assert not (tmp_path / 'no.age').exists()
