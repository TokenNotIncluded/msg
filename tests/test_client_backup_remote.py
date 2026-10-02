"""Container, inert-manifest and exclusive-download boundaries."""

import base64
import hashlib
import os
import shutil
import stat
import subprocess
from copy import deepcopy

import httpx
import pytest

from msg.client_backup_remote import (
    MAX_MANIFEST_BYTES,
    _read_ciphertext,
    detect_encryption,
    discover_backup,
    fetch_backup,
    validate_manifest,
)
from msg.core.codec import canonical
from msg.core.errors import Failure


@pytest.fixture
def ciphertext(tmp_path):
    age, keygen = shutil.which('age'), shutil.which('age-keygen')
    if age is None or keygen is None:
        pytest.skip('real age required')
    identity = tmp_path / 'offline.agekey'
    subprocess.run([keygen, '-o', str(identity)], check=True, capture_output=True)
    recipient = subprocess.run(
        [keygen, '-y', str(identity)], check=True, capture_output=True
    ).stdout.strip()
    return subprocess.run(
        [age, '-r', recipient.decode()],
        input=b'identity backup fixture',
        check=True,
        capture_output=True,
    ).stdout


def manifest(data):
    digest = hashlib.sha256(data).hexdigest()
    return {
        'schema': 'msg.identity-backup/1',
        'server': 'https://msg.example',
        'subject_id': 'u_fixture',
        'key_id': 'ed25519:fixture',
        'created_at': '2026-10-02T01:02:03Z',
        'encryption': {'format': 'age'},
        'archive_format': 'msg.account-backup/1',
        'file': {
            'path': f'/@alice/BACKUP-{digest[:16]}.age',
            'sha256': digest,
            'size': len(data),
        },
    }


def test_real_age_binary_and_armor_structure(ciphertext):
    assert detect_encryption(ciphertext) == 'age'
    encoded = base64.b64encode(ciphertext)
    armor = (
        b'-----BEGIN AGE ENCRYPTED FILE-----\n'
        + b'\n'.join(encoded[offset : offset + 64] for offset in range(0, len(encoded), 64))
        + b'\n-----END AGE ENCRYPTED FILE-----\n'
    )
    assert detect_encryption(armor) == 'age'
    assert detect_encryption(armor.replace(b'\n', b'\r\n')) == 'age'


@pytest.mark.parametrize(
    'data',
    [
        b'AGE-SECRET-KEY-1plaintext',
        b'{"private_key":"plaintext"}',
        b'age-encryption.org/v1\n--- ' + b'A' * 43 + b'\n' + b'X' * 40,
        b'age-encryption.org/v1\n-> X25519 anything\nnot base64\n',
        b'-----BEGIN AGE ENCRYPTED FILE-----\nYWdl\n-----END AGE ENCRYPTED FILE-----\n',
        b'-----BEGIN PGP PRIVATE KEY BLOCK-----\nplaintext\n',
        # A literal data packet is not an encrypted container.
        bytes([0xCB, 4]) + b'text',
    ],
)
def test_plaintext_spoofed_headers_and_literal_packets_rejected(data):
    with pytest.raises(Failure):
        detect_encryption(data)


def test_ciphertext_fifo_is_rejected_without_waiting_for_writer(tmp_path):
    fifo = tmp_path / 'blocked.age'
    os.mkfifo(fifo, mode=0o600)
    # A plain O_RDONLY open would hang here because no writer exists.
    with pytest.raises(Failure, match='unsafe_backup_file'):
        _read_ciphertext(fifo)


def test_real_gpg_encrypted_container_binary_and_armor(tmp_path):
    gpg = shutil.which('gpg')
    if gpg is None:
        pytest.skip('real gpg required')
    home = tmp_path / 'gpg'
    home.mkdir(mode=0o700)
    for armor in (False, True):
        command = [
            gpg,
            '--homedir',
            str(home),
            '--batch',
            '--pinentry-mode',
            'loopback',
            '--passphrase-fd',
            '0',
            '--symmetric',
            '--cipher-algo',
            'AES256',
        ]
        if armor:
            command.append('--armor')
        plaintext = tmp_path / 'fixture.txt'
        plaintext.write_bytes(b'encrypted fixture')
        data = subprocess.run(
            [*command, '--output', '-', str(plaintext)],
            input=b'fixture-only-passphrase\n',
            check=True,
            capture_output=True,
        ).stdout
        assert detect_encryption(data) == 'gpg'
        # Appending arbitrary data must not silently turn into an accepted archive.
        with pytest.raises(Failure):
            detect_encryption(data + b'plaintext')


@pytest.mark.parametrize(
    'field,value',
    [
        ('schema', 'wrong'),
        ('server', 'https://other.example'),
        ('subject_id', []),
        ('key_id', None),
        ('created_at', '2026-02-30T00:00:00Z'),
        ('created_at', '2026-10-02T01:02:03+00:00'),
        ('encryption', {'format': []}),
        ('encryption', {'format': 'age', 'command': 'rm -rf'}),
        ('archive_format', []),
        ('recovery_hint', 'secret\ncommand'),
        ('recovery_hint', 'x' * 501),
    ],
)
def test_invalid_manifest_types_and_inert_fields(ciphertext, field, value):
    proposed = manifest(ciphertext)
    proposed[field] = value
    with pytest.raises(Failure):
        validate_manifest(proposed, server='https://msg.example', handle='alice')


@pytest.mark.parametrize(
    'path',
    [
        'https://evil.example/backup.age',
        '/@bob/files/BACKUP-same.age',
        '/@alice/files/../BACKUP-x.age',
        '/@alice/files/%2e%2e/BACKUP-x.age',
        '/@alice/BACKUP-x.age?command=execute',
    ],
)
def test_manifest_rejects_alternate_download_paths(ciphertext, path):
    proposed = manifest(ciphertext)
    proposed['file']['path'] = path
    with pytest.raises(Failure, match='invalid_backup_path'):
        validate_manifest(proposed, server='https://msg.example', handle='alice')


@pytest.mark.parametrize('size', [True, 0, -1, 8 * 1024 * 1024 + 1, '5'])
def test_manifest_rejects_invalid_sizes(ciphertext, size):
    proposed = manifest(ciphertext)
    proposed['file']['size'] = size
    with pytest.raises(Failure):
        validate_manifest(proposed, server='https://msg.example', handle='alice')


@pytest.mark.asyncio
async def test_anonymous_download_checks_body_bounds_hash_and_exclusive_output(
    tmp_path, ciphertext
):
    details = manifest(ciphertext)
    responses = {'manifest': canonical(details), 'cipher': ciphertext}
    seen = []

    def route(request):
        seen.append(request)
        assert request.headers.get('authorization') is None
        if request.url.path.endswith('/meta'):
            return httpx.Response(200, content=canonical({'id': 'u_fixture', 'type': 'user'}))
        return httpx.Response(
            200,
            content=responses[
                'manifest' if request.url.path.endswith('BACKUP.json/raw') else 'cipher'
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as http:
        output = tmp_path / 'download.age'
        assert await fetch_backup('https://msg.example', '@alice', output, http=http) == details
        assert output.read_bytes() == ciphertext and stat.S_IMODE(output.stat().st_mode) == 0o600
        with pytest.raises(Failure, match='backup_output_exists'):
            await fetch_backup('https://msg.example', '@alice', output, http=http)
        assert output.read_bytes() == ciphertext
        symlink = tmp_path / 'existing-link.age'
        symlink.symlink_to(output)
        with pytest.raises(Failure, match='backup_output_exists'):
            await fetch_backup('https://msg.example', '@alice', symlink, http=http)
        linked_parent = tmp_path / 'linked'
        linked_parent.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(Failure, match='unsafe_backup_output'):
            await fetch_backup(
                'https://msg.example', '@alice', linked_parent / 'new.age', http=http
            )
        assert not (tmp_path / 'new.age').exists()
        assert not list(tmp_path.glob('.msg-backup-*'))
        bad = tmp_path / 'bad.age'
        responses['cipher'] = b'wrong ciphertext'
        with pytest.raises(Failure, match='backup_size_mismatch'):
            await fetch_backup('https://msg.example', '@alice', bad, http=http)
        assert not bad.exists()
        responses['cipher'] = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]
        with pytest.raises(Failure, match='backup_hash_mismatch'):
            await fetch_backup('https://msg.example', '@alice', bad, http=http)
        assert not bad.exists()
        responses['cipher'] = ciphertext
        with pytest.raises(Failure, match='backup_hash_mismatch'):
            await fetch_backup(
                'https://msg.example', '@alice', bad, expected_sha256='0' * 64, http=http
            )
        assert not bad.exists()
    assert all(request.url.host == 'msg.example' for request in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'response,error',
    [
        (
            httpx.Response(302, headers={'location': 'https://evil.example'}),
            'backup_redirect_not_allowed',
        ),
        (httpx.Response(200, content=b'X' * (MAX_MANIFEST_BYTES + 1)), 'backup_download_too_large'),
        (httpx.Response(503), 'backup_download_failed'),
    ],
)
async def test_discovery_rejects_redirects_oversized_and_unavailable(response, error):
    calls = []

    def route(request):
        calls.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as http:
        with pytest.raises(Failure, match=error):
            await discover_backup('https://msg.example', 'alice', http=http)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_manifest_is_distinct_from_invalid_manifest(ciphertext):
    details = deepcopy(manifest(ciphertext))
    response = httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        assert await discover_backup('https://msg.example', 'alice', http=http) is None
        details['file']['sha256'] = 'bad'
        response = httpx.Response(200, content=canonical(details))
        with pytest.raises(Failure):
            await discover_backup('https://msg.example', 'alice', http=http)


@pytest.mark.asyncio
async def test_discovery_binds_manifest_to_actual_profile_subject(ciphertext):
    details = manifest(ciphertext)

    def route(request):
        return httpx.Response(
            200,
            content=canonical(
                details
                if request.url.path.endswith('/raw')
                else {
                    'id': 'u_someone_else',
                    'type': 'user',
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as http:
        with pytest.raises(Failure, match='backup_subject_mismatch'):
            await discover_backup('https://msg.example', 'alice', http=http)
