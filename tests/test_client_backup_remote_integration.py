"""Public encrypted backups use real age, signatures, PostgreSQL and HTTP."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
from copy import deepcopy

import httpx
import pytest
from test_service import NOW

from msg.client import ClientState, MsgClient
from msg.client_backup_remote import discover_backup, fetch_backup, publish_backup
from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import b64, canonical
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.fixture
def age_cipher(tmp_path):
    age, keygen = shutil.which('age'), shutil.which('age-keygen')
    if age is None or keygen is None:
        pytest.skip('real age and age-keygen are required for encrypted backup acceptance')
    identity = tmp_path / 'fixture.agekey'
    subprocess.run([keygen, '-o', str(identity)], check=True, capture_output=True)
    recipient = subprocess.run(
        [keygen, '-y', str(identity)], check=True, capture_output=True, text=True
    ).stdout.strip()

    def encrypt(label):
        plaintext = b'msg encrypted backup integration fixture: ' + label.encode()
        ciphertext = subprocess.run(
            [age, '-r', recipient], input=plaintext, check=True, capture_output=True
        ).stdout
        target = tmp_path / (label + '.age')
        target.write_bytes(ciphertext)
        target.chmod(0o600)
        return target, plaintext

    try:
        yield encrypt, identity, age
    finally:
        identity.unlink(missing_ok=True)


async def client_for(app, directory, username, *, requests=None):
    async def record(request):
        if requests is not None and request.method == 'POST':
            body = json.loads(request.content)
            if 'operation' in body:
                requests.append(body)

    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
        event_hooks={'request': [record]},
    )
    client = MsgClient(
        ClientState(directory, server=app.settings.service_url),
        HTTPTransport(app.settings.service_url, http=http),
        clock=lambda: NOW,
    )
    client.checked(await client.register(username))
    return client, http


def metadata(client, path):
    return {
        'server': client.state.server,
        'subject_id': client.state.subject,
        'key_id': client.state.signer.key_id,
        'ciphertext_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'created_at': '2026-09-27T00:00:00Z',
        'archive_format': 'msg.account-backup/1',
    }


async def meta(client, path):
    return client.checked(await client.call('discovery.get', {'id': path, 'view': 'meta'})).data


async def event_count(app):
    async with app.metadata.transaction(write=False) as tx:
        return tx.one('SELECT COUNT(*) FROM events')[0]


@pytest.mark.asyncio
async def test_publish_and_anonymous_fetch_real_age_and_repeat_without_cipher_rewrite(
    installed, tmp_path, age_cipher
):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'backup-owner')
    encrypt, identity, age = age_cipher
    cipher, plaintext = encrypt('first')
    details = metadata(client, cipher)
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    try:
        await RemoteAgents(client).create('restore-check')
        private_before = await meta(client, '/@backup-owner/files')
        assert await discover_backup(client.state.server, 'backup-owner', http=anonymous) is None
        published = await publish_backup(
            client,
            cipher,
            details,
            recovery_hint='Use the offline fixture identity; this contains no secret.',
            request_id='backup-publish-first',
        )
        digest = details['ciphertext_sha256']
        cipher_path = '/@backup-owner/BACKUP-' + digest[:16] + '.age'
        assert published['schema'] == 'msg.identity-backup/1'
        assert published['server'] == client.state.server
        assert published['subject_id'] == client.state.subject
        assert published['key_id'] == client.state.signer.key_id
        assert published['encryption']['format'] == 'age'
        assert published['file'] == {
            'path': cipher_path,
            'sha256': digest,
            'size': cipher.stat().st_size,
        }
        cipher_before = await meta(client, cipher_path)
        manifest_meta = await meta(client, '/@backup-owner/BACKUP.json')
        assert cipher_before['owner'] == manifest_meta['owner'] == client.state.subject
        private_after = await meta(client, '/@backup-owner/files')
        assert int(private_before['mode'], 8) == int(private_after['mode'], 8) == 0o700
        assert private_after['generation'] == private_before['generation']
        for private_path in ('/@backup-owner/files', '/@backup-owner/files/agents'):
            assert (await anonymous.get(private_path)).status_code == 403
        private_json = await anonymous.get(
            '/@backup-owner/files/agents/restore-check/agent.json/raw'
        )
        assert private_json.status_code == 403
        assert 'restore-check' not in private_json.text
        assert (await anonymous.get('/@backup-owner/BACKUP.json')).status_code == 200
        assert (
            await discover_backup(client.state.server, '@backup-owner', http=anonymous) == published
        )

        output = tmp_path / 'download.age'
        old_umask = os.umask(0)
        try:
            fetched = await fetch_backup(
                client.state.server,
                'backup-owner',
                output,
                expected_sha256=digest,
                http=anonymous,
            )
        finally:
            os.umask(old_umask)
        assert fetched == published
        assert output.read_bytes() == cipher.read_bytes()
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert (
            subprocess.run(
                [age, '-d', '-i', str(identity), str(output)], check=True, capture_output=True
            ).stdout
            == plaintext
        )
        # A new request ID still reuses the immutable ciphertext resource.
        assert (
            await publish_backup(
                client,
                cipher,
                details,
                recovery_hint=published['recovery_hint'],
                request_id='backup-publish-repeat',
            )
            == published
        )
        cipher_after = await meta(client, cipher_path)
        assert (cipher_after['id'], cipher_after['generation'], cipher_after['revision']) == (
            cipher_before['id'],
            cipher_before['generation'],
            cipher_before['revision'],
        )
        files = client.checked(
            await client.call('discovery.list', {'parent': '/@backup-owner', 'type': 'file'})
        ).data['items']
        assert [item['name'] for item in files if item['name'].startswith('BACKUP-')] == [
            cipher_path.rsplit('/', 1)[1]
        ]
    finally:
        await anonymous.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_publish_updates_manifest_with_expected_generation_preserves_old_cipher(
    installed, tmp_path, age_cipher
):
    app, _ = installed
    requests = []
    client, http = await client_for(app, tmp_path / 'owner', 'backup-update', requests=requests)
    encrypt, _, _ = age_cipher
    first_cipher, _ = encrypt('before')
    second_cipher, _ = encrypt('after')
    try:
        first = await publish_backup(
            client, first_cipher, metadata(client, first_cipher), request_id='publish-before'
        )
        before = await meta(client, '/@backup-update/BACKUP.json')
        old_cipher = await meta(client, first['file']['path'])
        requests.clear()
        second = await publish_backup(
            client, second_cipher, metadata(client, second_cipher), request_id='publish-after'
        )
        assert first['file']['path'] != second['file']['path']
        writes = [item for item in requests if item['operation'] == 'file.write']
        assert len(writes) == 1
        assert writes[0]['arguments']['id'] == before['id']
        assert writes[0]['arguments']['base_revision'] == before['revision']
        assert writes[0]['expected_generations'] == [[before['id'], before['generation']]]
        after = await meta(client, '/@backup-update/BACKUP.json')
        assert after['id'] == before['id']
        assert after['generation'] == before['generation'] + 1
        assert (await meta(client, first['file']['path']))['revision'] == old_cipher['revision']
        assert await discover_backup(client.state.server, 'backup-update', http=http) == second
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_publish_rejects_plaintext_and_other_identity_without_mutation(
    installed, tmp_path, age_cipher
):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'backup-reject')
    encrypt, _, _ = age_cipher
    cipher, _ = encrypt('encrypted')
    plaintext = tmp_path / 'not-encrypted.age'
    plaintext.write_bytes(b'not a ciphertext, even with the .age extension')
    plaintext.chmod(0o600)
    try:
        before = await event_count(app)
        with pytest.raises(Failure):
            await publish_backup(client, plaintext, metadata(client, plaintext))
        assert await event_count(app) == before
        for changed in (
            {'subject_id': 'u_another_user'},
            {'server': 'https://another.example'},
            {'key_id': 'another-key'},
        ):
            with pytest.raises(Failure):
                await publish_backup(client, cipher, {**metadata(client, cipher), **changed})
            assert await event_count(app) == before
        missing = await client.call('discovery.get', {'id': '/@backup-reject/BACKUP.json'})
        assert missing.status == 'error' and missing.error.code == 'not_found'
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_fetch_rejects_cross_profile_paths_and_wrong_digest_without_output(
    installed, tmp_path, age_cipher
):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'backup-path')
    encrypt, _, _ = age_cipher
    cipher, _ = encrypt('source')
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    try:
        published = await publish_backup(client, cipher, metadata(client, cipher))
        output = tmp_path / 'must-not-exist.age'
        with pytest.raises(Failure):
            await fetch_backup(
                client.state.server,
                'backup-path',
                output,
                expected_sha256='0' * 64,
                http=anonymous,
            )
        assert not output.exists()
        tampered = deepcopy(published)
        tampered['file']['path'] = '/@other-profile/files/' + cipher.name
        saved = await meta(client, '/@backup-path/BACKUP.json')
        client.checked(
            await client.call(
                'file.write',
                {
                    'id': saved['id'],
                    'base_revision': saved['revision'],
                    'data': b64(canonical(tampered)),
                    'media_type': 'application/json',
                },
                expected=((saved['id'], saved['generation']),),
            )
        )
        with pytest.raises(Failure):
            await fetch_backup(client.state.server, 'backup-path', output, http=anonymous)
        assert not output.exists()
    finally:
        await anonymous.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_fetch_detects_real_cipher_mutation_and_preserves_existing_output(
    installed, tmp_path, age_cipher
):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'backup-integrity')
    encrypt, _, _ = age_cipher
    cipher, _ = encrypt('intact')
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    try:
        published = await publish_backup(client, cipher, metadata(client, cipher))
        existing = tmp_path / 'existing.age'
        existing.write_bytes(b'preserve this independently existing output')
        existing.chmod(0o600)
        with pytest.raises(Failure, match='backup_output_exists'):
            await fetch_backup(client.state.server, 'backup-integrity', existing, http=anonymous)
        assert existing.read_bytes() == b'preserve this independently existing output'
        assert stat.S_IMODE(existing.stat().st_mode) == 0o600

        saved = await meta(client, published['file']['path'])
        damaged = bytearray(cipher.read_bytes())
        damaged[-1] ^= 1
        client.checked(
            await client.call(
                'file.write',
                {
                    'id': saved['id'],
                    'base_revision': saved['revision'],
                    'data': b64(bytes(damaged)),
                    'media_type': 'application/octet-stream',
                },
                expected=((saved['id'], saved['generation']),),
            )
        )
        output = tmp_path / 'damaged-download.age'
        with pytest.raises(Failure, match='backup_hash_mismatch'):
            await fetch_backup(client.state.server, 'backup-integrity', output, http=anonymous)
        assert not output.exists()
        assert list(tmp_path.glob('.msg-backup-*')) == []
    finally:
        await anonymous.aclose()
        await http.aclose()
