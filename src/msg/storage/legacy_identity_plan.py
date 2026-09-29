"""Public-proof inventory for 0.22.1; never imports authentication authority.

Wire formats match installed crypto.py SHA256
235d2736927dbdf2a0d37cd28d78b4b46324b6aba902461f9eca5b1ec22fc83e.
This inventory verifies signatures, not a grant of current authorization.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from msg.core.codec import canonical, decode, digest, parse_time
from msg.core.errors import Failure, require
from msg.core.models import CapabilityGrant, Principal, Scope, Signature
from msg.plugins.common import create_resource
from msg.security.crypto import verify
from msg.security.quarantine import require_live_authority

PURPOSE = 'legacy-identity-archive-v1'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _public(value):
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32:
        raise ValueError('invalid public key')
    return raw, hashlib.sha256(raw).hexdigest()


def _verify(key, signature, payload):
    try:
        raw, _ = _public(key)
        Ed25519PublicKey.from_public_bytes(raw).verify(
            base64.b64decode(signature, validate=True), payload
        )
        return True
    except ValueError, TypeError, InvalidSignature:
        return False


def _post_payload(row, files):
    version = row['sig_version']
    signer = row['actor_id'] or row['author_id']
    fields = [
        ('action', 'post.create' if version == 1 else 'post.edit'),
        ('signer_id', signer),
        ('version', str(version)),
    ]
    if version == 1:
        fields += [
            ('nonce', row['sig_nonce']),
            ('issued', str(row['sig_issued'])),
            ('owner_id', signer),
        ]
    else:
        fields += [('post_id', str(row['id'])), ('owner_id', row['author_id'])]
    fields += [(key, row[key]) for key in ('board', 'name', 'title', 'body')]
    fields += [
        ('reply_to', '' if row['reply_to'] is None else str(row['reply_to'])),
        ('files', _json(files)),
    ]
    return b'msg.lmm.best/request/v1\n' + b''.join(
        key.encode() + b':' + str(len(value.encode())).encode() + b':' + value.encode() + b'\n'
        for key, value in fields
    )


def _certificate_body(body):
    data = json.loads(body)
    require(isinstance(data, dict) and data.get('v') == 1, 'legacy_certificate_invalid')
    _, fingerprint = _public(data['subject_key'])
    require(fingerprint == data['subject_id'], 'legacy_certificate_invalid')
    grants = {}
    for grant in data['grants']:
        grants.setdefault(grant['topic'], set()).update(grant['actions'])
    normalized = {
        key: data[key]
        for key in (
            'delegate',
            'issuer_id',
            'issuer_serial',
            'not_after',
            'not_before',
            'serial',
            'subject_id',
            'v',
        )
    }
    normalized['subject_key'] = base64.b64encode(_public(data['subject_key'])[0]).decode()
    normalized['grants'] = [
        {'topic': topic, 'actions': sorted(actions)} for topic, actions in sorted(grants.items())
    ]
    require(
        type(data['not_before']) is int
        and type(data['not_after']) is int
        and data['not_after'] > data['not_before'],
        'legacy_certificate_invalid',
    )
    return normalized


def identity_plan(snapshot: Path, sha256: str, *, at: int, root_public_key: str | None = None):
    """Read selected public columns only; return sensitive identifiers to the caller.

    Write the returned plan ONLY to a protected file. summary() is safe for logs.
    Old token hashes, custody keys/ciphertext and keystore blobs are never selected.
    Root anchor is explicit; missing anchor yields unknown, never trusted.
    """
    path = Path(snapshot).resolve()

    def check():
        with path.open('rb') as stream:
            require(
                hashlib.file_digest(stream, 'sha256').hexdigest() == sha256,
                'legacy_snapshot_changed',
            )

    check()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    identities = {}

    def observe(subject, key=None):
        subject = subject or '__anonymous__'
        record = identities.setdefault(
            subject,
            {
                'old_id': subject,
                'key_fingerprints': set(),
                'key_mismatch': False,
                'post_signatures': Counter(),
                'certificate_serials': [],
                'authority_enabled': False,
                'kind': 'historical_record',
            },
        )
        if key:
            try:
                _, fingerprint = _public(key)
                record['key_fingerprints'].add(fingerprint)
                record['key_mismatch'] |= fingerprint != subject
            except ValueError, TypeError:
                record['key_mismatch'] = True
        return record

    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        revoked = {row[0] for row in db.execute('SELECT serial FROM revocations')}
        certs = {}
        for row in db.execute(
            'SELECT serial,issuer_serial,issuer_id,subject_id,subject_key,body,signature FROM certificates ORDER BY serial'
        ):
            row = dict(row)
            observe(row['subject_id'], row['subject_key'])['certificate_serials'].append(
                row['serial']
            )
            try:
                body = _certificate_body(row['body'])
                require(
                    all(
                        body[k] == row[k]
                        for k in (
                            'serial',
                            'issuer_serial',
                            'issuer_id',
                            'subject_id',
                            'subject_key',
                        )
                    ),
                    'legacy_certificate_columns_mismatch',
                )
            except Failure, ValueError, TypeError, KeyError, AttributeError:
                body = None
            certs[row['serial']] = (row, body)
        certificates = []
        for serial, (row, _body) in certs.items():
            chain = []
            current = serial
            reason = 'root_anchor_unknown'
            seen = set()
            while current != 'root':
                if current in seen or current not in certs:
                    reason = 'chain_cycle_or_missing'
                    break
                seen.add(current)
                item, normalized = certs[current]
                chain.append(current)
                if normalized is None:
                    reason = 'invalid_certificate'
                    break
                if current in revoked:
                    reason = 'revoked'
                    break
                if at < normalized['not_before']:
                    reason = 'not_yet_valid'
                    break
                if at >= normalized['not_after']:
                    reason = 'expired'
                    break
                parent = item['issuer_serial']
                key = (
                    root_public_key
                    if parent == 'root'
                    else (certs[parent][0]['subject_key'] if parent in certs else None)
                )
                if key is None:
                    reason = 'root_anchor_unknown' if parent == 'root' else 'chain_missing'
                    break
                try:
                    matches = _public(key)[1] == item['issuer_id']
                except ValueError, TypeError:
                    matches = False
                if not matches or not _verify(
                    key, item['signature'], b'msg.lmm.best/cert/v1\n' + _json(normalized).encode()
                ):
                    reason = 'invalid_signature'
                    break
                current = parent
            else:
                reason = 'signature_chain_verified'
            # This status is cryptographic continuity, NOT grant/delegation authorization.
            certificates.append({
                'serial': serial,
                'subject_id': row['subject_id'],
                'issuer_serial': row['issuer_serial'],
                'chain': chain,
                'status': reason,
                'authority_enabled': False,
            })
        for table, attachment_table in (
            ('posts', 'attachments'),
            ('archived_posts', 'archived_attachments'),
        ):
            files = {}
            for attachment in db.execute(
                'SELECT post_id,name,content_type,nbytes,sha256 FROM '
                + attachment_table
                + ' ORDER BY post_id,slot'
            ):
                files.setdefault(attachment['post_id'], []).append({
                    'name': attachment['name'],
                    'type': attachment['content_type'],
                    'bytes': attachment['nbytes'],
                    'sha256': attachment['sha256'],
                })
            fields = 'id,board,name,title,body,author_id,author_key,actor_id,actor_key,signature,sig_version,sig_nonce,sig_issued,reply_to'
            for row in db.execute('SELECT ' + fields + ' FROM ' + table + ' ORDER BY id'):
                record = observe(row['author_id'], row['author_key'])
                actor = observe(
                    row['actor_id'] or row['author_id'], row['actor_key'] or row['author_key']
                )
                status = 'unsigned'
                if row['signature']:
                    try:
                        key = row['actor_key'] or row['author_key']
                        _, fingerprint = _public(key)
                        payload = _post_payload(row, files.get(row['id'], []))
                        status = (
                            'verified'
                            if (
                                fingerprint == (row['actor_id'] or row['author_id'])
                                and _verify(key, row['signature'], payload)
                            )
                            else 'invalid'
                        )
                    except ValueError, TypeError, AttributeError:
                        status = 'unprovable'
                record['post_signatures'][status] += 1
                if actor is not record:
                    actor['post_signatures']['actor_' + status] += 1
        for table, column, key_column in (
            ('identity_names', 'author_id', None),
            ('name_claims', 'author_id', 'public_key'),
            ('profiles', 'author_id', None),
            ('custody_identities', 'author_id', 'public_key'),
        ):
            columns = column + (',' + key_column if key_column else '')
            for row in db.execute('SELECT ' + columns + ' FROM ' + table):
                observe(row[column], row[key_column] if key_column else None)
        check()
    finally:
        db.close()
    records = []
    for subject, record in sorted(identities.items()):
        record['key_fingerprints'] = sorted(record['key_fingerprints'])
        record['post_signatures'] = dict(sorted(record['post_signatures'].items()))
        record['classification'] = (
            'anonymous'
            if subject == '__anonymous__'
            else 'key_conflict'
            if record['key_mismatch'] or len(record['key_fingerprints']) > 1
            else 'public_key_observed'
            if record['key_fingerprints']
            else 'unproven'
        )
        records.append(record)
    result = {
        'format': PURPOSE,
        'source_sha256': sha256,
        'evaluated_at': at,
        'root_fingerprint': _public(root_public_key)[1] if root_public_key else None,
        'identities': records,
        'certificates': certificates,
        'authority_enabled': False,
        'private_ciphertext_imported': False,
    }
    return result


def summary(plan):
    return {
        'plan_digest': digest(plan),
        'identity_count': len(plan['identities']),
        'classifications': dict(Counter(r['classification'] for r in plan['identities'])),
        'certificate_statuses': dict(Counter(r['status'] for r in plan['certificates'])),
        'post_signature_statuses': dict(
            sum((Counter(r['post_signatures']) for r in plan['identities']), Counter())
        ),
        'authority_enabled': False,
    }


def approval_payload(
    *, plan_digest, service, parent, parent_generation, operator, mappings, expires_at
):
    return {
        'format': PURPOSE,
        'plan_digest': plan_digest,
        'target_service': service,
        'parent': parent,
        'parent_generation': parent_generation,
        'operator': operator,
        'mappings': mappings,
        'expires_at': expires_at,
        'visibility': 'private',
        'authority_enabled': False,
    }


async def import_identity_records(app, plan, approval, signature):
    """Archive historical identities as private files, NEVER Subject/auth records.

    Root chooses either null (unmapped historical identity) or an existing registered
    subject for each old ID. Association is recorded as a Root decision, not proof
    that the destination subject owns the historical private key.
    """
    verify(
        app.certificates.root_public_key,
        canonical(approval),
        decode(Signature, signature),
        purpose=PURPOSE,
    )
    expected = approval_payload(
        **{
            k: approval.get(k)
            for k in (
                'plan_digest',
                'parent',
                'parent_generation',
                'operator',
                'mappings',
                'expires_at',
            )
        },
        service=approval.get('target_service'),
    )
    require(
        approval == expected and approval['plan_digest'] == digest(plan),
        'legacy_identity_approval_invalid',
    )
    require(approval['target_service'] == app.settings.service_url, 'legacy_service_mismatch')
    now = app.clock()
    require(
        now < parse_time(approval['expires_at']) <= now + timedelta(hours=24),
        'legacy_approval_expired',
    )
    require(
        plan.get('format') == PURPOSE and plan.get('authority_enabled') is False,
        'legacy_identity_plan_invalid',
    )
    mappings = approval['mappings']
    require(
        isinstance(mappings, dict) and set(mappings) == {r['old_id'] for r in plan['identities']},
        'legacy_identity_mapping_incomplete',
    )
    context = SimpleNamespace(
        now=now,
        entry='local_admin',
        principal=Principal(
            actor=approval['operator'],
            subject=approval['operator'],
            credential_id=None,
            method='local',
            certificates=(),
            ceiling=(
                CapabilityGrant(
                    capability='content.basic',
                    version=1,
                    scope=Scope(resource_id=approval['parent'], descendants=True),
                    operations=frozenset({'legacy.identity_archive@1'}),
                    constraints={},
                ),
            ),
        ),
    )
    request = SimpleNamespace(operation='legacy.identity_archive', contract_version=1, arguments={})
    async with app.metadata.transaction(write=True) as tx:
        require_live_authority(tx)
        operator = await tx.subject(approval['operator'])
        require(operator.kind == 'registered', 'legacy_operator_must_be_registered')
        for target in mappings.values():
            require(target is None or isinstance(target, str), 'legacy_identity_mapping_invalid')
            if target is not None:
                require(
                    (await tx.subject(target)).kind == 'registered', 'legacy_mapping_target_invalid'
                )
        parent = await tx.resource(approval['parent'])
        require(
            parent.owner == operator.resource_id
            and parent.mode == 0o700
            and parent.generation == approval['parent_generation'],
            'legacy_parent_not_private',
        )
        require(
            not tx.one('SELECT id FROM resources WHERE parent=?', (parent.id,)),
            'legacy_parent_not_empty',
        )
        key = 'legacy-identity-archive:' + approval['plan_digest']
        require(tx.setting(key) is None, 'legacy_already_imported')
        for index, record in enumerate(plan['identities']):
            body = {
                'record': record,
                'source_sha256': plan['source_sha256'],
                'root_selected_target': mappings[record['old_id']],
                'mapping_is_key_ownership_proof': False,
                'authority_enabled': False,
            }
            await create_resource(
                app,
                context,
                request,
                tx,
                parent=parent.id,
                type='file',
                name=f'historical-identity-{index}.json',
                mode=0o600,
                body=canonical(body),
                media_type='application/json',
            )
        await create_resource(
            app,
            context,
            request,
            tx,
            parent=parent.id,
            type='file',
            name='historical-certificate-status.json',
            mode=0o600,
            body=canonical({'certificates': plan['certificates'], 'authority_enabled': False}),
            media_type='application/json',
        )
        report = {**summary(plan), 'parent': parent.id, 'approval_digest': digest(approval)}
        tx.set_setting(key, {'report': report, 'approval': approval, 'signature': signature})
        return report


def main():
    """Offline plan command: exclusive 0600 output, counts/digest only on stdout."""
    import argparse
    import os
    import time

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument(
        '--root-public-key',
        type=Path,
        help='Explicit old Root public key file, never a private key',
    )
    parser.add_argument('--at', type=int, default=None)
    args = parser.parse_args()
    root = args.root_public_key.read_text().strip() if args.root_public_key else None
    plan = identity_plan(
        args.snapshot,
        args.sha256,
        at=int(time.time()) if args.at is None else args.at,
        root_public_key=root,
    )
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(plan, stream, indent=2)
    print(json.dumps(summary(plan), sort_keys=True))


if __name__ == '__main__':
    main()
