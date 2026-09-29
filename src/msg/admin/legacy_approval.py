"""Prepare and physically approve only the three inert legacy migration contracts."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from collections import Counter
from datetime import timedelta
from pathlib import Path

from msg.admin.root import require_local_console, root_envelope
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import canonical, digest, loads, parse_time, wire
from msg.core.errors import require
from msg.security.crypto import Ed25519Signer, open_private_key
from msg.security.quarantine import require_live_authority
from msg.security.root_files import read_private, rotation_lock
from msg.storage import (
    legacy_git_import as git,
    legacy_identity_plan as identities,
    legacy_resource_import as content,
)


def write_new(path, value):
    path = Path(path)
    require(not path.exists() and not path.is_symlink(), 'approval_destination_exists')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(canonical(value))
        stream.write(b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path):
    return loads(read_private(path, limit=16 * 1024 * 1024))


def recognized_payload(value):
    require(isinstance(value, dict), 'legacy_approval_invalid')
    kind = value.get('format')
    common = {
        key: value.get(key) for key in ('parent', 'parent_generation', 'operator', 'expires_at')
    }
    if kind == content.PURPOSE:
        expected = content.approval_payload(
            sha256=value.get('source_sha256'),
            service=value.get('target_service'),
            identities=value.get('identities'),
            **common,
        )
    elif kind == git.PURPOSE:
        expected = git.git_approval(
            bundle_sha256=value.get('bundle_sha256'),
            refs_digest=value.get('refs_digest'),
            default_ref=value.get('default_ref'),
            name=value.get('name'),
            service=value.get('target_service'),
            **common,
        )
    elif kind == identities.PURPOSE:
        expected = identities.approval_payload(
            plan_digest=value.get('plan_digest'),
            mappings=value.get('mappings'),
            service=value.get('target_service'),
            **common,
        )
    else:
        require(False, 'unsupported_legacy_approval_purpose')
    require(value == expected, 'legacy_approval_invalid')
    return kind


async def validate_and_review(app, approval):
    kind = recognized_payload(approval)
    require(approval['target_service'] == app.settings.service_url, 'legacy_service_mismatch')
    require(
        app.clock() < parse_time(approval['expires_at']) <= app.clock() + timedelta(hours=24),
        'legacy_approval_expired',
    )
    mappings = approval.get('identities', approval.get('mappings', {}))
    require(isinstance(mappings, dict), 'legacy_mapping_invalid')
    async with app.metadata.transaction(write=False) as tx:
        require_live_authority(tx)
        require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
        holder = await tx.subject(approval['operator'])
        require(holder.kind == 'registered', 'legacy_operator_must_be_registered')
        holder_resource = await tx.resource(holder.resource_id)
        parent = await tx.resource(approval['parent'])
        require(
            parent.owner == holder.resource_id
            and parent.mode == 0o700
            and parent.state == 'active'
            and parent.generation == approval['parent_generation'],
            'legacy_parent_not_private',
        )
        require(
            app.registry.resource_type(parent.type, parent.type_version).container,
            'not_a_container',
        )
        if kind != git.PURPOSE:
            require(
                not tx.one('SELECT id FROM resources WHERE parent=?', (parent.id,)),
                'legacy_parent_not_empty',
            )
        target_counts = Counter()
        for target in mappings.values():
            require(target is None or isinstance(target, str), 'legacy_mapping_invalid')
            if target is not None:
                require(
                    (await tx.subject(target)).kind == 'registered', 'legacy_mapping_target_invalid'
                )
                target_counts[target] += 1
        return {
            'purpose': kind,
            'approval_digest': digest(approval),
            'artifact_binding': {
                key: approval[key]
                for key in (
                    'source_sha256',
                    'bundle_sha256',
                    'refs_digest',
                    'default_ref',
                    'name',
                    'plan_digest',
                )
                if key in approval
            },
            'target_service': app.settings.service_url,
            'root_public_key_digest': digest(app.certificates.root_public_key),
            'real_holder': {'subject': holder.resource_id, 'handle': holder_resource.name},
            'private_parent': {
                'id': parent.id,
                'path': await tx.path(parent.id),
                'generation': parent.generation,
            },
            'historical_identity_count': len(mappings),
            'unmapped_count': sum(v is None for v in mappings.values()),
            'explicit_new_subject_associations': dict(target_counts),
            'legacy_authority_enabled': False,
            'ownership_proof_claimed': False,
            'expires_at': approval['expires_at'],
        }


async def inspect_approval(config_dir, approval):
    app = Application(load_settings(config_dir))
    try:
        await app.load()
        return await validate_and_review(app, approval), app.certificates.root_public_key
    finally:
        await app.close()


def sign_approval(config_dir, source, destination):
    # This check precedes input parsing, trust material and every PIN prompt.
    require_local_console(config_dir)
    require(
        not Path(destination).exists() and not Path(destination).is_symlink(),
        'approval_destination_exists',
    )
    approval = read_json(source)
    review, public = asyncio.run(inspect_approval(config_dir, approval))
    print(canonical(review).decode())
    phrase = 'APPROVE LEGACY IMPORT ' + digest(approval)
    require(input('Type ' + phrase + ': ') == phrase, 'approval_cancelled')
    settings = load_settings(config_dir)
    with rotation_lock(settings.root_private_dir):
        envelope = loads(read_private(root_envelope(config_dir)))
        private = open_private_key(envelope, getpass.getpass('Root PIN/passphrase: '))
        signer = Ed25519Signer.from_bytes(private)
        require(signer.public_key == public, 'root_key_mismatch')
        result = {
            'approval': approval,
            'signature': wire(
                signer.sign(canonical(approval), purpose=recognized_payload(approval))
            ),
        }
        write_new(destination, result)
    return {
        'status': 'legacy_approval_signed',
        'purpose': approval['format'],
        'approval_digest': digest(approval),
    }


async def prepare(args):
    app = Application(load_settings(args.config_dir))
    try:
        await app.load()
        async with app.metadata.transaction(write=False) as tx:
            parent = await tx.resource(args.parent)
        common = {
            'service': app.settings.service_url,
            'parent': args.parent,
            'parent_generation': parent.generation,
            'operator': args.operator,
            'expires_at': wire(app.clock() + timedelta(hours=2)),
        }
        if args.command == 'prepare-content':
            needed = content.identity_requirements(
                args.snapshot, args.sha256, {}, summary_only=False
            )['required']
            mapping = dict.fromkeys(needed) if args.unmapped else read_json(args.mappings)
            require(set(mapping) == set(needed), 'legacy_identity_mapping_incomplete')
            approval = content.approval_payload(sha256=args.sha256, identities=mapping, **common)
        elif args.command == 'prepare-git':
            manifest = read_json(args.manifest)
            verified = git.verify_bundle(
                args.bundle,
                manifest['bundle_sha256'],
                protected_work=app.settings.server.staging_dir,
            )
            require(
                verified['refs'] == manifest['refs'] and manifest.get('refs_stable') is True,
                'legacy_git_refs_mismatch',
            )
            approval = git.git_approval(
                bundle_sha256=verified['bundle_sha256'],
                refs_digest=verified['refs_digest'],
                default_ref=manifest['default_ref'],
                name=args.name,
                **common,
            )
        else:
            plan = read_json(args.plan)
            needed = {row['old_id'] for row in plan['identities']}
            mapping = dict.fromkeys(needed) if args.unmapped else read_json(args.mappings)
            require(set(mapping) == needed, 'legacy_identity_mapping_incomplete')
            approval = identities.approval_payload(
                plan_digest=digest(plan), mappings=mapping, **common
            )
        review = await validate_and_review(app, approval)
        write_new(args.output, approval)
        return review
    finally:
        await app.close()


async def apply_identities(args):
    app = Application(load_settings(args.config_dir))
    try:
        await app.load()
        envelope = read_json(args.signed_approval)
        return await identities.import_identity_records(
            app, read_json(args.plan), envelope['approval'], envelope['signature']
        )
    finally:
        await app.close()


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument('--config-dir', type=Path, required=True)
    sub = command.add_subparsers(dest='command', required=True)
    for kind in ('content', 'git', 'identities'):
        item = sub.add_parser('prepare-' + kind)
        item.add_argument('--operator', required=True)
        item.add_argument('--parent', required=True)
        item.add_argument('--output', type=Path, required=True)
        if kind == 'content':
            item.add_argument('--snapshot', type=Path, required=True)
            item.add_argument('--sha256', required=True)
        elif kind == 'git':
            item.add_argument('--bundle', type=Path, required=True)
            item.add_argument('--manifest', type=Path, required=True)
            item.add_argument('--name', required=True)
        else:
            item.add_argument('--plan', type=Path, required=True)
        if kind != 'git':
            mapping = item.add_mutually_exclusive_group(required=True)
            mapping.add_argument('--mappings', type=Path)
            mapping.add_argument('--unmapped', action='store_true')
    sign = sub.add_parser('sign')
    sign.add_argument('--draft', type=Path, required=True)
    sign.add_argument('--output', type=Path, required=True)
    apply = sub.add_parser('apply-identities')
    apply.add_argument('--plan', type=Path, required=True)
    apply.add_argument('--signed-approval', type=Path, required=True)
    return command


def main():
    args = parser().parse_args()
    if args.command == 'sign':
        result = sign_approval(args.config_dir, args.draft, args.output)
    elif args.command == 'apply-identities':
        result = asyncio.run(apply_identities(args))
    else:
        result = asyncio.run(prepare(args))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
