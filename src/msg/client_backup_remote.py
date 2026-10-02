"""Explicit public hosting and anonymous recovery of encrypted identity backups.

Only ciphertext and a small, inert manifest are published. Container recognition
checks structure, not decryptability or the choice/strength of a passphrase.
"""

import base64
import binascii
import hashlib
import os
import re
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from msg.core.codec import b64, canonical, loads
from msg.core.errors import Failure, require
from msg.service_origin import service_origin

SCHEMA = 'msg.identity-backup/1'
MAX_MANIFEST_BYTES = 16 * 1024
MAX_CIPHERTEXT_BYTES = 8 * 1024 * 1024
# The default signed transport accepts 1 MiB, including base64 and proof.
MAX_PUBLISH_BYTES = 512 * 1024
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,160}')
_SHA256 = re.compile(r'[0-9a-f]{64}')


def normalize_handle(handle):
    require(type(handle) is str, 'invalid_backup_handle')
    handle = handle.removeprefix('@')
    require(
        re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}(?:~[a-z0-9]{1,32})?', handle),
        'invalid_backup_handle',
    )
    return handle


def validate_manifest(value, *, server, handle):
    """Validate inert public metadata without accepting arbitrary URLs or commands."""
    server, handle = service_origin(server), normalize_handle(handle)
    fields = {
        'schema',
        'server',
        'subject_id',
        'key_id',
        'created_at',
        'encryption',
        'archive_format',
        'file',
    }
    require(
        type(value) is dict and fields <= set(value) <= fields | {'recovery_hint'},
        'invalid_backup_manifest',
    )
    require(value['schema'] == SCHEMA, 'invalid_backup_manifest')
    require(value['server'] == server, 'backup_server_mismatch')
    for field in ('subject_id', 'key_id'):
        require(
            type(value[field]) is str and _IDENTIFIER.fullmatch(value[field]),
            'invalid_backup_manifest',
        )
    created = value['created_at']
    require(
        type(created) is str
        and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', created),
        'invalid_backup_manifest',
    )
    try:
        datetime.fromisoformat(created)
    except ValueError as exc:
        raise Failure('invalid_backup_manifest') from exc
    encryption = value['encryption']
    require(
        type(encryption) is dict
        and set(encryption) == {'format'}
        and type(encryption['format']) is str
        and encryption['format'] in {'age', 'gpg'},
        'invalid_backup_manifest',
    )
    require(
        type(value['archive_format']) is str
        and value['archive_format'] in {'msg.account-backup/1', 'external'},
        'invalid_backup_manifest',
    )
    file = value['file']
    require(
        type(file) is dict and set(file) == {'path', 'sha256', 'size'}, 'invalid_backup_manifest'
    )
    sha = file['sha256']
    require(type(sha) is str and _SHA256.fullmatch(sha), 'invalid_backup_manifest')
    require(
        type(file['size']) is int and 0 < file['size'] <= MAX_CIPHERTEXT_BYTES,
        'invalid_backup_manifest',
    )
    require(
        file['path'] == f'/@{handle}/BACKUP-{sha[:16]}.{encryption["format"]}',
        'invalid_backup_path',
    )
    if 'recovery_hint' in value:
        hint = value['recovery_hint']
        require(
            type(hint) is str
            and len(hint) <= 500
            and all(ord(char) >= 32 and ord(char) != 127 for char in hint),
            'invalid_backup_hint',
        )
    require(len(canonical(value)) <= MAX_MANIFEST_BYTES, 'backup_manifest_too_large')
    return value


def _base64(value, *, padded=False):
    try:
        decoded = base64.b64decode(
            value if padded else value + b'=' * (-len(value) % 4), validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise Failure('invalid_backup_ciphertext') from exc
    canonical_value = base64.b64encode(decoded)
    require(
        (canonical_value if padded else canonical_value.rstrip(b'=')) == value,
        'invalid_backup_ciphertext',
    )
    return decoded


def _unarmor(data, label):
    begin, end = b'-----BEGIN ' + label + b'-----', b'-----END ' + label + b'-----'
    lines = data.replace(b'\r\n', b'\n').split(b'\n')
    while lines and lines[-1] == b'':
        lines.pop()
    require(len(lines) >= 3 and lines[0] == begin and lines[-1] == end, 'invalid_backup_ciphertext')
    body = lines[1:-1]
    if label == b'PGP MESSAGE':
        while body and body[0] != b'':
            require(
                re.fullmatch(rb'[A-Za-z][A-Za-z0-9-]*: [\x20-\x7e]*', body[0]),
                'invalid_backup_ciphertext',
            )
            body.pop(0)
        require(body and body.pop(0) == b'', 'invalid_backup_ciphertext')
        if body and body[-1].startswith(b'='):
            # CRC is optional, but a supplied checksum must match the container.
            crc = _base64(body.pop()[1:], padded=True)
            require(len(crc) == 3, 'invalid_backup_ciphertext')
        else:
            crc = None
    else:
        crc = None
    require(body and all(0 < len(line) <= 64 for line in body), 'invalid_backup_ciphertext')
    decoded = _base64(b''.join(body), padded=True)
    if crc is not None:
        value = 0xB704CE
        for byte in decoded:
            value ^= byte << 16
            for _ in range(8):
                value <<= 1
                if value & 0x1000000:
                    value ^= 0x1864CFB
        require((value & 0xFFFFFF).to_bytes(3) == crc, 'invalid_backup_ciphertext')
    return decoded


def _age_container(data):
    if data.startswith(b'-----BEGIN AGE ENCRYPTED FILE-----'):
        data = _unarmor(data, b'AGE ENCRYPTED FILE')
    require(data.startswith(b'age-encryption.org/v1\n'), 'invalid_backup_ciphertext')
    offset, stanzas = len(b'age-encryption.org/v1\n'), 0
    while True:
        end = data.find(b'\n', offset, min(len(data), 65536))
        require(end >= 0, 'invalid_backup_ciphertext')
        line, offset = data[offset:end], end + 1
        if line.startswith(b'--- '):
            require(stanzas > 0 and len(_base64(line[4:])) == 32, 'invalid_backup_ciphertext')
            # Nonce plus the authentication tag of the final (possibly empty) chunk.
            require(len(data) - offset >= 32, 'invalid_backup_ciphertext')
            return
        require(
            re.fullmatch(rb'-> [\x21-\x7e]+(?: [\x21-\x7e]+)*', line),
            'invalid_backup_ciphertext',
        )
        stanzas += 1
        require(stanzas <= 128, 'invalid_backup_ciphertext')
        body = bytearray()
        while True:
            end = data.find(b'\n', offset, min(len(data), 65536))
            require(end >= 0, 'invalid_backup_ciphertext')
            line, offset = data[offset:end], end + 1
            require(re.fullmatch(rb'[A-Za-z0-9+/]{0,64}', line), 'invalid_backup_ciphertext')
            body.extend(line)
            if len(line) < 64:
                break
        _base64(bytes(body))


def _pgp_packet(data, offset):
    require(offset < len(data) and data[offset] & 0x80, 'invalid_backup_ciphertext')
    header, offset = data[offset], offset + 1
    if header & 0x40:
        tag = header & 0x3F
        pieces = []
        while True:
            require(offset < len(data), 'invalid_backup_ciphertext')
            length, offset = data[offset], offset + 1
            partial = 224 <= length < 255
            if length < 192:
                size = length
            elif length < 224:
                require(offset < len(data), 'invalid_backup_ciphertext')
                size, offset = ((length - 192) << 8) + data[offset] + 192, offset + 1
            elif partial:
                size = 1 << (length & 0x1F)
            else:
                require(offset + 4 <= len(data), 'invalid_backup_ciphertext')
                size, offset = int.from_bytes(data[offset : offset + 4]), offset + 4
            require(offset + size <= len(data), 'invalid_backup_ciphertext')
            pieces.append(data[offset : offset + size])
            offset += size
            if not partial:
                return tag, b''.join(pieces), offset
    tag, kind = (header >> 2) & 15, header & 3
    require(kind != 3, 'invalid_backup_ciphertext')
    width = (1, 2, 4)[kind]
    require(offset + width <= len(data), 'invalid_backup_ciphertext')
    size, offset = int.from_bytes(data[offset : offset + width]), offset + width
    require(offset + size <= len(data), 'invalid_backup_ciphertext')
    return tag, data[offset : offset + size], offset + size


def _gpg_container(data):
    if data.startswith(b'-----BEGIN PGP MESSAGE-----'):
        data = _unarmor(data, b'PGP MESSAGE')
    offset, keys = 0, 0
    while offset < len(data):
        tag, body, offset = _pgp_packet(data, offset)
        if tag in {1, 3}:
            require(len(body) >= (10 if tag == 1 else 4), 'invalid_backup_ciphertext')
            require(body[0] in ({3, 6} if tag == 1 else {4, 5, 6}), 'invalid_backup_ciphertext')
            keys += 1
            require(keys <= 128, 'invalid_backup_ciphertext')
        else:
            # Refuse legacy unprotected encrypted-data and plaintext literal packets.
            require(
                tag in {18, 20}
                and keys > 0
                and len(body) >= 33
                and body[0] in ({1, 2} if tag == 18 else {1})
                and offset == len(data),
                'invalid_backup_ciphertext',
            )
            return
    raise Failure('invalid_backup_ciphertext')


def detect_encryption(data):
    require(
        type(data) is bytes and 0 < len(data) <= MAX_CIPHERTEXT_BYTES, 'invalid_backup_ciphertext'
    )
    if data.startswith((b'age-encryption.org/', b'-----BEGIN AGE ENCRYPTED FILE-----')):
        _age_container(data)
        return 'age'
    if data.startswith(b'-----BEGIN PGP MESSAGE-----') or data[0] & 0x80:
        _gpg_container(data)
        return 'gpg'
    raise Failure('backup_encryption_required')


def _read_ciphertext(path):
    path = Path(path).expanduser().absolute()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'unsafe_backup_file')
            require(
                before.st_uid == os.getuid() and before.st_mode & 0o022 == 0, 'unsafe_backup_file'
            )
            require(0 < before.st_size <= MAX_PUBLISH_BYTES, 'backup_publish_too_large')
            data = stream.read(MAX_PUBLISH_BYTES + 1)
            after = os.fstat(stream.fileno())
            require(
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                and len(data) == before.st_size,
                'backup_file_changed',
            )
    except OSError as exc:
        raise Failure('unsafe_backup_file') from exc
    return data, before.st_mtime


async def _call(client, operation, args, **kwargs):
    signer = client.signer_override or client.state.signer
    require(signer is not None, 'signing_identity_required')
    return client.checked(await client.call(operation, args, signer=signer, **kwargs)).data


async def _meta(client, path):
    try:
        return await _call(client, 'discovery.get', {'id': path, 'view': 'meta'})
    except Failure as exc:
        if exc.code == 'not_found':
            return None
        raise


def _public_owned(meta, subject):
    mode = int(meta['mode'], 8) if type(meta['mode']) is str else meta['mode']
    require(
        meta['owner'] == subject
        and meta['type'] == 'file'
        and meta['state'] == 'active'
        and mode & 0o044 == 0o044,
        'backup_public_namespace_conflict',
    )


async def publish_backup(client, ciphertext_path, metadata, *, recovery_hint=None, request_id=None):
    """Publish only on explicit invocation; never upload a plaintext identity archive."""
    data, modified_at = _read_ciphertext(ciphertext_path)
    format = detect_encryption(data)
    sha = hashlib.sha256(data).hexdigest()
    require(type(metadata) is dict, 'invalid_backup_metadata')
    require(metadata.get('server') == client.state.server, 'backup_server_mismatch')
    require(metadata.get('subject_id') == client.state.subject, 'backup_subject_mismatch')
    key = client.signer_override or client.state.signer
    require(key is not None and metadata.get('key_id') == key.key_id, 'backup_key_mismatch')
    require(metadata.get('ciphertext_sha256', sha) == sha, 'backup_hash_mismatch')
    require(metadata.get('encryption_format', format) == format, 'backup_encryption_mismatch')
    identity = await _call(
        client, 'discovery.get', {'id': client.state.subject, 'fields': ['id', 'name']}
    )
    require(identity['id'] == client.state.subject, 'backup_subject_mismatch')
    handle = normalize_handle(identity['name'])
    path = f'/@{handle}/BACKUP-{sha[:16]}.{format}'
    manifest = {
        'schema': SCHEMA,
        'server': client.state.server,
        'subject_id': client.state.subject,
        'key_id': key.key_id,
        'created_at': metadata.get('created_at')
        or datetime.fromtimestamp(modified_at, UTC).isoformat().replace('+00:00', 'Z'),
        'encryption': {'format': format},
        'archive_format': metadata.get('archive_format', 'msg.account-backup/1'),
        'file': {'path': path, 'sha256': sha, 'size': len(data)},
    }
    if recovery_hint is not None:
        manifest['recovery_hint'] = recovery_hint
    elif metadata.get('recovery_hint') is not None:
        manifest['recovery_hint'] = metadata['recovery_hint']
    validate_manifest(manifest, server=client.state.server, handle=handle)
    request_id = request_id or 'backup-' + uuid.uuid4().hex
    for parent in ('/@' + handle,):
        container = await _meta(client, parent)
        mode = (
            int(container['mode'], 8)
            if container is not None and type(container['mode']) is str
            else (container or {}).get('mode', 0)
        )
        require(
            container is not None and container['state'] == 'active' and mode & 0o055 == 0o055,
            'backup_public_namespace_conflict',
        )
    existing = await _meta(client, path)
    if existing is None:
        result = await client.call(
            'file.create',
            {
                'parent': f'/@{handle}',
                'name': path.rsplit('/', 1)[1],
                'data': b64(data),
                'media_type': 'application/octet-stream',
            },
            signer=key,
            request_id=request_id + '-cipher',
        )
        if result.status == 'error' and result.error.code == 'constraint_conflict':
            existing = await _meta(client, path)
        else:
            client.checked(result)
            existing = await _meta(client, path)
    require(existing is not None, 'backup_public_namespace_conflict')
    _public_owned(existing, client.state.subject)
    require(
        existing.get('digest') == 'sha256:' + sha and existing.get('size') == len(data),
        'backup_ciphertext_conflict',
    )
    manifest_path = f'/@{handle}/BACKUP.json'
    old = await _meta(client, manifest_path)
    if old is None:
        await _call(
            client,
            'file.create',
            {
                'parent': '/@' + handle,
                'name': 'BACKUP.json',
                'data': b64(canonical(manifest)),
                'media_type': 'application/json',
            },
            request_id=request_id + '-manifest',
        )
    else:
        _public_owned(old, client.state.subject)
        if old.get('digest') != 'sha256:' + hashlib.sha256(canonical(manifest)).hexdigest():
            await _call(
                client,
                'file.write',
                {
                    'id': old['id'],
                    'base_revision': old['revision'],
                    'data': b64(canonical(manifest)),
                    'media_type': 'application/json',
                },
                expected=((old['id'], old['generation']),),
                request_id=request_id + '-manifest',
            )
    final = await _meta(client, manifest_path)
    require(final is not None, 'backup_public_namespace_conflict')
    _public_owned(final, client.state.subject)
    return manifest


async def _download(http, url, *, limit, missing=False):
    try:
        async with http.stream('GET', url, follow_redirects=False, timeout=15) as response:
            require(not response.is_redirect, 'backup_redirect_not_allowed')
            if missing and response.status_code == 404:
                return None
            require(
                response.status_code == 200,
                'backup_download_failed',
                details={'status': response.status_code},
            )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                require(len(body) <= limit, 'backup_download_too_large')
            return bytes(body)
    except httpx.HTTPError as exc:
        raise Failure('backup_download_failed') from exc


async def discover_backup(server, handle, *, http=None):
    server, handle = service_origin(server), normalize_handle(handle)
    if http is None:
        async with httpx.AsyncClient(
            trust_env=False, timeout=15, follow_redirects=False
        ) as anonymous:
            return await discover_backup(server, handle, http=anonymous)
    data = await _download(
        http, server + f'/@{handle}/BACKUP.json/raw', limit=MAX_MANIFEST_BYTES, missing=True
    )
    if data is None:
        return None
    try:
        value = loads(data)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise Failure('invalid_backup_manifest') from exc
    manifest = validate_manifest(value, server=server, handle=handle)
    owner = await _download(http, server + f'/@{handle}/meta', limit=MAX_MANIFEST_BYTES)
    try:
        profile = loads(owner)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise Failure('invalid_backup_manifest') from exc
    require(
        type(profile) is dict
        and profile.get('type') == 'user'
        and profile.get('id') == manifest['subject_id'],
        'backup_subject_mismatch',
    )
    return manifest


def _exclusive_output(output, data):
    output = Path(output).expanduser().absolute()
    temporary = '.msg-backup-' + uuid.uuid4().hex
    directory = None
    try:
        directory = os.open(output.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for part in output.parent.parts[1:]:
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # link creates the destination exclusively; an existing file/symlink is never replaced.
        os.link(
            temporary,
            output.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise Failure('backup_output_exists') from exc
    except OSError as exc:
        raise Failure('unsafe_backup_output') from exc
    finally:
        if directory is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory)


async def fetch_backup(server, handle, output, *, expected_sha256=None, http=None):
    server, handle = service_origin(server), normalize_handle(handle)
    require(
        expected_sha256 is None
        or type(expected_sha256) is str
        and _SHA256.fullmatch(expected_sha256),
        'invalid_backup_hash',
    )
    if http is None:
        async with httpx.AsyncClient(
            trust_env=False, timeout=15, follow_redirects=False
        ) as anonymous:
            return await fetch_backup(
                server, handle, output, expected_sha256=expected_sha256, http=anonymous
            )
    manifest = await discover_backup(server, handle, http=http)
    require(manifest is not None, 'backup_not_found')
    file = manifest['file']
    require(expected_sha256 is None or expected_sha256 == file['sha256'], 'backup_hash_mismatch')
    data = await _download(http, server + file['path'] + '/raw', limit=MAX_CIPHERTEXT_BYTES)
    require(len(data) == file['size'], 'backup_size_mismatch')
    require(hashlib.sha256(data).hexdigest() == file['sha256'], 'backup_hash_mismatch')
    require(
        detect_encryption(data) == manifest['encryption']['format'], 'backup_encryption_mismatch'
    )
    _exclusive_output(output, data)
    return manifest
