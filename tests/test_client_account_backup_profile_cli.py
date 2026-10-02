"""CLI publication, anonymous profile download, and original-subject restore together."""

import shutil

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.atomic_file import durable_write
from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.paths import ClientPaths
from msg.security.age_keys import generate_age_key
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


def environment(monkeypatch, directory):
    for scope in ('CONFIG', 'DATA', 'STATE', 'CACHE'):
        monkeypatch.setenv(f'XDG_{scope}_HOME', str(directory / scope.lower()))
    monkeypatch.delenv('MSG_SERVER', raising=False)


async def test_real_cli_publish_anonymous_fetch_restore_and_signed_original_subject(
    installed, tmp_path, monkeypatch, capsys
):
    if shutil.which('age') is None:
        pytest.skip('real age CLI is required')
    from msg import client_backup_remote as remote

    app, _ = installed
    server = app.settings.service_url
    environment(monkeypatch, tmp_path / 'before')
    state = ClientState(server=server, account='original')
    identity, recipient = generate_age_key()
    recovery = tmp_path / 'outside-agent.agekey'
    durable_write(recovery, (identity + '\n').encode())
    real_fetch = remote.fetch_backup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=server
    ) as http:
        client = MsgClient(state, HTTPTransport(server, http=http), clock=lambda: NOW)
        registration = await client.register('profile-backup-cli-fixture')
        assert registration.status == 'ok'
        subject, key = state.subject, state.signer.key_id
        anonymous_calls = []

        async def anonymous_fetch(*args, **kwargs):
            async def no_credentials(request):
                assert 'authorization' not in request.headers
                assert 'x-msg-request' not in request.headers
                assert 'cookie' not in request.headers
                anonymous_calls.append(request.url.path)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(app)),
                base_url=server,
                event_hooks={'request': [no_credentials]},
            ) as anonymous:
                return await real_fetch(*args, http=anonymous, **kwargs)

        monkeypatch.setattr(remote, 'fetch_backup', anonymous_fetch)
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda origin: HTTPTransport(origin, http=http))
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )

        async def invoke(*args):
            parsed = cli.parser().parse_args(['--server', server, *args])
            assert await cli.run(parsed) == 0
            return loads(capsys.readouterr().out.strip())

        published = await invoke(
            '--account',
            'original',
            'account',
            'backup',
            '--recipient',
            recipient,
            '--publish',
        )
        assert published['public_ciphertext'] is True and 'output' not in published
        manifest = published['published']
        assert manifest['subject_id'] == subject and manifest['key_id'] == key
        assert published['ciphertext_sha256'] == 'sha256:' + manifest['file']['sha256']
        home = '/@profile-backup-cli-fixture'
        assert manifest['file']['path'].startswith(home + '/BACKUP-')
        private_files = await client.call('discovery.get', {'id': home + '/files', 'view': 'meta'})
        assert private_files.status == 'ok'
        mode = private_files.data['mode']
        assert (int(mode, 8) if isinstance(mode, str) else mode) == 0o700

        # Existing ciphertext can be republished with an explicitly selected archive format.
        downloaded = tmp_path / 'downloaded.age'
        fetched = await invoke(
            'account',
            'fetch',
            '--from',
            '@profile-backup-cli-fixture',
            '--output',
            str(downloaded),
            '--expected-sha256',
            published['ciphertext_sha256'],
        )
        assert fetched['decrypted'] is False and fetched['manifest'] == manifest
        assert downloaded.stat().st_mode & 0o777 == 0o600
        republished = await invoke(
            '--account',
            'original',
            'account',
            'publish',
            '--input',
            str(downloaded),
            '--encryption',
            'age',
            '--archive-format',
            'external',
        )
        assert republished['manifest']['archive_format'] == 'external'
        # Restore the standard archive declaration after the external-format action check.
        await invoke(
            '--account',
            'original',
            'account',
            'publish',
            '--input',
            str(downloaded),
            '--encryption',
            'age',
            '--archive-format',
            'msg.account-backup/1',
        )
        environment(monkeypatch, tmp_path / 'rebuilt')
        restored = await invoke(
            'account',
            'restore',
            'recovered',
            '--from',
            '@profile-backup-cli-fixture',
            '--identity',
            str(recovery),
            '--expected-subject',
            subject,
            '--expected-key-id',
            key,
            '--expected-sha256',
            published['ciphertext_sha256'],
        )
        assert restored['subject_id'] == subject and restored['key_id'] == key
        assert restored['source_account'] == 'original' and restored['selected'] is False
        assert not (ClientPaths.discover(server=server).config / 'current-account.json').exists()
        current = ClientState(server=server, account='recovered')
        signed = MsgClient(current, HTTPTransport(server, http=http), clock=lambda: NOW)
        result = await signed.call('discovery.get', {'id': subject, 'view': 'meta'})
        assert result.status == 'ok' and result.actor == subject
        assert current.signer.key_id == key
        assert home + '/BACKUP.json/raw' in anonymous_calls
        assert home + '/meta' in anonymous_calls
        assert manifest['file']['path'] + '/raw' in anonymous_calls


def test_restore_source_flags_are_exclusive_and_fetch_needs_no_private_key():
    with pytest.raises(SystemExit):
        cli.parser().parse_args([
            'account',
            'restore',
            'new',
            '--input',
            'old.age',
            '--from',
            '@alice',
            '--identity',
            'offline.key',
        ])
    args = cli.parser().parse_args([
        '--server',
        'https://example.org',
        'account',
        'fetch',
        '--from',
        '@alice',
        '--output',
        'cipher.age',
    ])
    assert args.account is None and args.key is None
