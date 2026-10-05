"""Run native wire code against the retained Python implementation, not a copy.

Usage: PYTHONPATH=src python rust/tests/test_python_parity.py --binary PATH
Only deterministic public test keys are used. No server, database, or credentials.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import random
import struct
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from msg.core.codec import b64, canonical, decode, digest, loads, unb64, wire
from msg.core.models import Signature
from msg.core.packet import decode_packet
from msg.core.requests import payload_fields, request_for, signing_bytes
from msg.security.crypto import Ed25519Signer, key_id, subject_id, verify

# This seed is public test material, shared with the Rust example only.
SIGNER = Ed25519Signer.from_bytes(bytes(range(32)))
CASES: list[tuple[str, dict, dict]] = []


def add(group: str, command: dict, data: dict | None = None) -> None:
    expected = {"ok": False} if data is None else {"ok": True, "data": data}
    CASES.append((group, command, expected))


def canonical_case(raw: str) -> None:
    command = {"action": "canonical", "raw": raw}
    try:
        encoded = canonical(loads(raw))
    except (ValueError, TypeError, RecursionError):
        add("canonical_reject", command)
    else:
        add("canonical", command, {"canonical": encoded.decode(), "digest": digest(encoded)})


def request_case(raw: str, *, public: bool = False) -> None:
    command = {"action": "request", "raw": raw}
    if public:
        command["public"] = b64(SIGNER.public_key)
    try:
        packet = decode_packet(raw)
    except (ValueError, TypeError, RecursionError):
        add("request_reject", command)
        return
    correct_digest = digest(payload_fields(packet))
    verified = None
    if public:
        verified = False
        if packet.payload_digest == correct_digest and hasattr(packet.proof, "signature"):
            try:
                verify(SIGNER.public_key, signing_bytes(packet), packet.proof.signature, purpose="request")
            except (ValueError, TypeError):
                pass
            else:
                verified = True
    add("request", command, {
        "canonical": canonical(packet).decode(), "payload_digest": correct_digest,
        "signing": b64(signing_bytes(packet)), "digest_valid": packet.payload_digest == correct_digest,
        "verified": verified,
    })


def build_cases() -> None:
    raw_values = [
        'null', 'true', 'false', '0', '-0', '-0.0', '0.0', '1e-7', '1e-6', '1e-5',
        '1e-4', '1e15', '1e16', '1e20', '1e23', '5e-324', '-5e-324',
        '1.7976931348623157e308', '2.2250738585072014e-308', '1e-999', '-1e-999',
        '184467440737095516160000000', '-184467440737095516160000000', '9' * 4300,
        r'{"a":1,"\u0061":2}', r'[{"nested":1,"nested":2}]',
        'NaN', 'Infinity', '-Infinity', '1e999', '[1,]', '01', 'true false',
        r'"\ud800"', r'"\udfff"', '9' * 4301,
    ]
    raw_values += [json.dumps(value, ensure_ascii=True) for value in [
        {"😀": 1, "\ue000": 2, "a": "中文\n"}, {"z": [], "a": {"b": None}},
        "".join(chr(i) for i in range(32)) + "\"/\\\u007f\u2028\u2029",
        {"é": 1, "e\u0301": 2, "𐀀": 3}, [True, 1, 1.0, -0.0],
    ]]
    for raw in raw_values:
        canonical_case(raw)
    rng = random.Random(20261005)
    for _ in range(16_384):
        value = struct.unpack('!d', rng.getrandbits(64).to_bytes(8, 'big'))[0]
        if math.isfinite(value):
            canonical_case(repr(value))
    for power in range(-323, 309):
        value = float(f'1e{power}')
        for neighbor in [value, math.nextafter(value, 0.0), math.nextafter(value, math.inf)]:
            if math.isfinite(neighbor):
                canonical_case(repr(neighbor))
    for size in [0, 1, 2, 3, 31, 32, 64, 1024]:
        encoded = b64(bytes(rng.getrandbits(8) for _ in range(size)))
        add("base64", {"action": "base64", "raw": encoded}, {"encoded": encoded, "digest": digest(unb64(encoded))})
    for raw in ['YQ=', 'YQ==', 'YR', '+/', 'Y Q', 'a', '中文', '\nYQ']:
        try:
            unb64(raw)
        except ValueError:
            add("base64_reject", {"action": "base64", "raw": raw})
        else:
            raise AssertionError("invalid base64 reference case was accepted")

    for purpose in ['request', 'receipt', 'revision', 'temporary-key-possession-v1']:
        for payload in [b'', b'hello', '你好\x00世界'.encode(), canonical({"n": 1e-7, "i": 2**80})]:
            signature = SIGNER.sign(payload, purpose=purpose)
            add("sign", {"action": "sign", "payload": b64(payload), "purpose": purpose}, {
                "public": b64(SIGNER.public_key), "key_id": key_id(SIGNER.public_key),
                "subject_id": subject_id(SIGNER.public_key), "signature": wire(signature),
            })
            command = {"action": "verify", "public": b64(SIGNER.public_key), "payload": b64(payload),
                       "signature": wire(signature), "purpose": purpose}
            add("verify", command, {"verified": True})
            add("verify_reject", dict(command, payload=b64(payload + b'!')))
            add("verify_reject", dict(command, purpose=purpose + '-wrong'))
            add("verify_reject", dict(command, public=b64(bytes(32))))

    request = request_for(
        'discovery.get', {'id': 'r_agents', 'fields': ['id', 'revision', 'content'], 'n': 1e-7, 'big': 2**90},
        'https://msg.lmm.best', signer=SIGNER, subject=subject_id(SIGNER.public_key),
        request_id='rust-wire-public-fixture', expires_at=datetime(2026, 10, 5, tzinfo=UTC),
        expected=(('r_agents', 0),), source='msg',
    )
    base = wire(request)
    request_case(canonical(base).decode(), public=True)
    for key, value in [
        ('source', 'mcp'), ('expires_at', '2026-10-05T00:01:00Z'),
        ('request_id', 'another'), ('target_service', 'https://other.example'),
        ('arguments', {'id': 'other'}), ('expected_generations', [['r_agents', 1]]),
        ('return_fields', ['data']), ('subject', None),
    ]:
        changed = copy.deepcopy(base)
        changed[key] = value
        request_case(canonical(changed).decode(), public=True)
    unsigned = dict(base, proof=None)
    unsigned.pop('source')
    unsigned.pop('return_fields')
    request_case(canonical(unsigned).decode())
    for timestamp in ['2026-10-05T00:00:00Z', '2026-10-05T00:00:00.1Z', '2026-10-05T00:00:00.123456789Z']:
        request_case(canonical(dict(base, expires_at=timestamp)).decode(), public=True)
    request_case(canonical(dict(base, proof={'credential_id': 'c_test', 'token': b64(b'test-only')})).decode())
    invalid = [
        ('extra', 1), ('protocol_version', True), ('contract_version', 1.0),
        ('operation', 'no_dot'), ('operation', 'discovery..get'), ('operation', 'Discovery.get'),
        ('request_id', ''), ('subject', 3), ('source', 'ssh'), ('arguments', []),
        ('expected_generations', [['r_agents', True]]), ('expected_generations', [['r_agents', -1]]),
        ('expected_generations', [['r_agents', 0], ['r_agents', 1]]),
        ('return_fields', ['id'] * 31), ('proof', {'credential_id': 'c', 'token': 'YR'}),
        ('expires_at', '2026-10-05T00:00:00+00:00'), ('payload_digest', 'sha256:bad'),
    ]
    for key, value in invalid:
        changed = copy.deepcopy(base)
        changed[key] = value
        request_case(canonical(changed).decode())
    for key in ['request_id', 'protocol_version', 'proof', 'arguments']:
        missing = copy.deepcopy(base)
        missing.pop(key)
        request_case(canonical(missing).decode())
    # Bounded Rust rollout profile is intentionally stricter, never silently truncated.
    for raw in [' ' * (1_048_576 + 1), '[' * 66 + '0' + ']' * 66]:
        add("bounded_reject", {"action": "canonical", "raw": raw})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    build_cases()
    inputs = ''.join(json.dumps(command, ensure_ascii=True) + '\n' for _, command, _ in CASES)
    result = subprocess.run([str(args.binary.resolve())], input=inputs, text=True, capture_output=True,
                            timeout=120, check=True)
    # JSONL is delimited by LF, not Unicode U+2028/U+2029 inside strings.
    if not result.stdout.endswith('\n'):
        raise AssertionError('native output is missing its final LF')
    outputs = result.stdout.split('\n')[:-1]
    if len(outputs) != len(CASES):
        raise AssertionError(f'native output count {len(outputs)} != {len(CASES)}')
    failures = []
    for index, ((group, command, expected), raw) in enumerate(zip(CASES, outputs, strict=True)):
        actual = json.loads(raw)
        passed = actual.get('ok') is expected['ok']
        if expected['ok']:
            passed = passed and actual.get('data') == expected['data']
        if not passed:
            failures.append({'index': index, 'group': group, 'input': command, 'expected': expected, 'actual': actual})
        # Explicit reverse check of signatures produced by Rust with the Python verifier.
        if passed and group == 'sign':
            data = actual['data']
            verify(unb64(data['public']), unb64(command['payload']), decode(Signature, data['signature']),
                   purpose=command['purpose'])
    report = {'cases': len(CASES), 'groups': dict(Counter(group for group, _, _ in CASES)),
              'failures': len(failures), 'first_failures': failures[:12]}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
