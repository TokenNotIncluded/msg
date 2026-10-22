"""Exercise the native encryption CLI against the actual Python implementation.

All keys and files are disposable. No server, account or network is used.
Usage: PYTHONPATH=src python rust/tests/test_crypto_parity.py --binary PATH
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from msg.client_secrets import encryption_keygen, write_private
from msg.core.codec import b64, canonical, unb64
from msg.security.sealed_box import FORMAT, decrypt, encrypt, validate_envelope

REPORT = []
PLAINTEXT_LIMIT = 524288
ENVELOPE_LIMIT = 1048576


def check(condition, name):
    if not condition:
        raise AssertionError(name)
    REPORT.append(name)


def native(binary, *arguments, rejected=False):
    result = subprocess.run(
        [str(binary), *(str(arg) for arg in arguments)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if rejected:
        if result.returncode <= 0 or b'panicked at' in result.stderr:
            raise AssertionError('invalid input was accepted or crashed the native process')
    elif result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors='replace'))
    return result.stdout


def python_rejects(content, private):
    try:
        decrypt(content, private)
    except ValueError, TypeError:
        return True
    return False


def interoperate(binary, root):
    native_key = root / 'rust.key'
    pair = json.loads(native(binary, 'keygen', native_key))
    private = native_key.read_bytes()
    public = X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
    check(len(private) == 32, 'keygen-raw-private-key')
    check(pair['format'] == FORMAT and pair['recipient'] == b64(public), 'keygen-python-public-key')
    check(pair['private_key_file'] == str(native_key), 'keygen-output-path')
    check(native_key.stat().st_mode & 0o777 == 0o600, 'keygen-private-mode')

    python_key = root / 'python.key'
    python_pair = encryption_keygen(python_key)
    payloads = [b'', '自由，加密\0\n'.encode() + bytes(range(256)), bytes(range(256)) * 2048]
    for index, plaintext in enumerate(payloads):
        source = root / f'source-{index}'
        source.write_bytes(plaintext)
        py_envelope = root / f'python-{index}.sealed'
        py_envelope.write_bytes(encrypt(plaintext, public))
        output = root / f'opened-{index}'
        native(binary, 'decrypt', native_key, py_envelope, output)
        check(output.read_bytes() == plaintext, f'python-to-rust-{index}')
        check(output.stat().st_mode & 0o777 == 0o600, f'plaintext-private-mode-{index}')

        rust_envelope = root / f'rust-{index}.sealed'
        native(binary, 'encrypt', python_pair['recipient'], source, rust_envelope)
        sealed = rust_envelope.read_bytes()
        check(decrypt(sealed, python_key.read_bytes()) == plaintext, f'rust-to-python-{index}')
        check(canonical(validate_envelope(sealed)) == sealed, f'canonical-envelope-{index}')
        check(rust_envelope.stat().st_mode & 0o777 == 0o600, f'ciphertext-private-mode-{index}')

    another = root / 'randomized.sealed'
    native(binary, 'encrypt', python_pair['recipient'], source, another)
    check(another.read_bytes() != sealed, 'fresh-encryption-randomness')
    check(
        decrypt(another.read_bytes(), python_key.read_bytes()) == plaintext,
        'fresh-envelope-opens',
    )

    # Python historically allowed larger plaintext; only new encryption is capped.
    historical = b'h' * (600 * 1024)
    legacy_envelope, legacy_output = root / 'historical.sealed', root / 'historical-opened'
    legacy_envelope.write_bytes(encrypt(historical, public))
    native(binary, 'decrypt', native_key, legacy_envelope, legacy_output)
    check(legacy_output.read_bytes() == historical, 'historical-envelope-above-plaintext-limit')
    return native_key, private, public


def rejected_envelopes(binary, root, key, private, public):
    envelope = encrypt(b'disposable secret', public)
    value = validate_envelope(envelope)
    invalid = {'invalid-json': b'{', 'duplicate-field': b'{"format":"x",' + envelope[1:]}
    for name in ['recipient', 'ephemeral', 'salt', 'nonce', 'ciphertext']:
        raw = bytearray(unb64(value[name]))
        raw[0] ^= 1
        invalid['changed-' + name] = canonical(value | {name: b64(raw)})
    invalid.update({
        'unknown-format': canonical(value | {'format': 'unknown'}),
        'unknown-field': canonical(value | {'extra': True}),
        'missing-field': canonical({name: item for name, item in value.items() if name != 'salt'}),
        'short-nonce': canonical(value | {'nonce': b64(bytes(11))}),
        'short-tag': canonical(value | {'ciphertext': b64(bytes(15))}),
        'low-order-ephemeral': canonical(value | {'ephemeral': b64(bytes(32))}),
    })
    for name, content in invalid.items():
        source, output = root / (name + '.sealed'), root / name
        source.write_bytes(content)
        check(python_rejects(content, private), 'python-rejects-' + name)
        native(binary, 'decrypt', key, source, output, rejected=True)
        check(not output.exists(), 'rust-rejects-without-output-' + name)

    valid = root / 'valid.sealed'
    valid.write_bytes(envelope)
    formatted, formatted_output = root / 'formatted.sealed', root / 'formatted-opened'
    formatted.write_text(json.dumps(dict(reversed(value.items())), indent=2), encoding='utf-8')
    native(binary, 'decrypt', key, formatted, formatted_output)
    check(formatted_output.read_bytes() == b'disposable secret', 'header-order-and-whitespace')
    wrong_key = root / 'wrong.key'
    encryption_keygen(wrong_key)
    output = root / 'wrong-recipient'
    native(binary, 'decrypt', wrong_key, valid, output, rejected=True)
    check(not output.exists(), 'wrong-recipient-no-plaintext')

    source = root / 'plaintext'
    source.write_bytes(b'never publish this for a low-order key')
    output = root / 'low-order-recipient'
    native(binary, 'encrypt', b64(bytes(32)), source, output, rejected=True)
    check(not output.exists(), 'low-order-recipient-no-ciphertext')
    return valid, source


def file_boundaries(binary, root, key, private, public, envelope, source):
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    saved_key = root / 'closed-stdout.key'
    try:
        result = subprocess.run(
            [str(binary), 'keygen', str(saved_key)],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    finally:
        os.close(write_fd)
    check(
        result.returncode == 1
        and b'panicked at' not in result.stderr
        and len(saved_key.read_bytes()) == 32,
        'closed-stdout-retains-private-key-without-panic',
    )
    native(binary, 'keygen', key, rejected=True)
    check(key.read_bytes() == private, 'keygen-never-overwrites-private-key')
    existing = root / 'existing'
    existing.write_bytes(b'keep existing bytes')
    for operation, identity, content in [
        ('encrypt', b64(public), source),
        ('decrypt', key, envelope),
    ]:
        native(binary, operation, identity, content, existing, rejected=True)
        check(existing.read_bytes() == b'keep existing bytes', operation + '-never-overwrites')
        link = root / (operation + '-symlink')
        link.symlink_to(existing)
        native(binary, operation, identity, content, link, rejected=True)
        check(
            link.is_symlink() and existing.read_bytes() == b'keep existing bytes',
            operation + '-symlink-intact',
        )

    linked_key = root / 'linked.key'
    linked_key.symlink_to(key)
    output = root / 'linked-key-output'
    native(binary, 'decrypt', linked_key, envelope, output, rejected=True)
    check(not output.exists(), 'symlink-private-key-rejected')

    key.chmod(0o644)
    try:
        output = root / 'unsafe-key-output'
        native(binary, 'decrypt', key, envelope, output, rejected=True)
        check(not output.exists(), 'publicly-readable-key-rejected')
    finally:
        key.chmod(0o600)
    for name, content in [('short', private[:-1]), ('long', private + b'x')]:
        bad_key, output = root / (name + '.key'), root / (name + '-key-output')
        write_private(bad_key, content)
        native(binary, 'decrypt', bad_key, envelope, output, rejected=True)
        check(not output.exists(), name + '-private-key-rejected')

    for operation, identity, size in [
        ('encrypt', b64(public), PLAINTEXT_LIMIT + 1),
        ('decrypt', key, ENVELOPE_LIMIT + 1),
    ]:
        oversized, output = root / (operation + '-oversized'), root / (operation + '-bounded')
        content = b'x' * size if operation == 'encrypt' else envelope.read_bytes()
        oversized.write_bytes(content + b' ' * (size - len(content)))
        native(binary, operation, identity, oversized, output, rejected=True)
        check(not output.exists(), operation + '-size-bound-no-output')

    fifo = root / 'fifo.key'
    os.mkfifo(fifo, 0o600)
    output = root / 'fifo-key-output'
    native(binary, 'decrypt', fifo, envelope, output, rejected=True)
    check(not output.exists(), 'nonregular-private-key-rejected-without-blocking')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    with tempfile.TemporaryDirectory(prefix='msg-crypto-parity-') as name:
        root = Path(name)
        key, private, public = interoperate(binary, root)
        envelope, source = rejected_envelopes(binary, root, key, private, public)
        file_boundaries(binary, root, key, private, public, envelope, source)
    report = {
        'checks': len(REPORT),
        'passed': REPORT,
        'scope': 'native local encryption; no server',
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
