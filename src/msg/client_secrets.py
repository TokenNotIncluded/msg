"""Local encryption helpers. Neither plaintext nor private keys reach a transport."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from msg.core.codec import b64, unb64, wire
from msg.core.errors import require
from msg.security.sealed_box import FORMAT, decrypt, encrypt, generate_key


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def encryption_keygen(path):
    private, public = generate_key()
    write_private(path, private)
    return {'format': FORMAT, 'recipient': b64(public), 'private_key_file': str(path)}


async def put_secret(client, name, path, recipient):
    # The msg envelope is bounded. Already encrypted large age/OpenPGP files can
    # instead use transfer + keystore.put(source=...), without loading them here.
    with Path(path).open('rb') as source:
        plaintext = source.read(524289)
    require(len(plaintext) <= 524288, 'use_streaming_age_or_openpgp')
    sealed = encrypt(plaintext, unb64(recipient, limit=32))
    with tempfile.TemporaryDirectory(prefix='encrypted-', dir=client.state.directory) as temp:
        ciphertext = Path(temp) / 'ciphertext'
        write_private(ciphertext, sealed)
        uploaded = client.checked(
            await client.upload(ciphertext, media_type='application/octet-stream')
        )
        return await client.call(
            'keystore.put', {'name': name, 'format': FORMAT, 'source': wire(uploaded.output)}
        )


async def get_secret(client, resource, output, private_key):
    path = Path(private_key)
    require(
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_uid == os.geteuid()
        and path.stat().st_mode & 0o077 == 0,
        'unsafe_encryption_key_permissions',
    )
    private = path.read_bytes()
    require(len(private) == 32, 'invalid_encryption_key')
    entry = client.checked(await client.call('keystore.get', {'id': resource}))
    require(entry.data['format'] == FORMAT, 'external_decryptor_required')
    require(entry.data['size'] <= 1048576, 'ciphertext_envelope_too_large')
    require(not Path(output).exists(), 'download_target_exists')
    with tempfile.TemporaryDirectory(prefix='encrypted-', dir=client.state.directory) as temp:
        ciphertext = Path(temp) / 'ciphertext'
        await client.download(entry.output, ciphertext)
        plaintext = decrypt(ciphertext.read_bytes(), private)
        write_private(output, plaintext)
    return {
        'id': entry.data['id'],
        'revision': entry.data['revision'],
        'path': str(output),
        'decrypted_locally': True,
    }
