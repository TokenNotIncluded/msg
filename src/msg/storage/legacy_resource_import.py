"""Root-approved offline import of legacy content into a private v4 namespace."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from msg.core.codec import b64, canonical, decode, digest, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import CapabilityGrant, Principal, Relation, ResourceRef, Scope, Signature
from msg.plugins.common import create_resource, revise_resource, validate_name
from msg.security.crypto import verify
from msg.security.quarantine import require_live_authority
from msg.storage.legacy_sqlite import _digest, preserve

PURPOSE = 'legacy-sqlite-content-import-v1'


def content_plan(snapshot: Path, sha256: str):
    manifest = preserve(snapshot, expected_sha256=sha256)
    db = sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        boards = [dict(row) for row in db.execute('SELECT * FROM boards')]
        posts = [
            (table, dict(row))
            for table in ('posts', 'archived_posts')
            for row in db.execute('SELECT * FROM ' + table)
        ]
        attachments = [
            (table, dict(row))
            for table in ('attachments', 'archived_attachments')
            for row in db.execute('SELECT * FROM ' + table)
        ]
        tags = [(row['post_id'], row['tag']) for row in db.execute('SELECT * FROM post_tags')]
        require(_digest(snapshot) == sha256, 'legacy_snapshot_changed')
        return manifest, boards, posts, attachments, tags
    finally:
        db.close()


def approval_payload(
    *, sha256, service, parent, parent_generation, operator, identities, expires_at
):
    """Sign these exact fields with the destination's existing offline Root key."""
    return {
        'format': PURPOSE,
        'source_sha256': sha256,
        'target_service': service,
        'parent': parent,
        'parent_generation': parent_generation,
        'operator': operator,
        'identities': identities,
        'expires_at': expires_at,
        'visibility': 'private',
        'legacy_authority': 'disabled',
        'external_jobs': 'disabled',
    }


async def import_content(app, snapshot: Path, approval: dict, signature: dict):
    """Import atomically using normal resource/CAS writes; never issue credentials.

    No signing key is generated or loaded here. The caller must supply an approval
    signed by the Root already pinned by the destination installation.
    """
    verify(
        app.certificates.root_public_key,
        canonical(approval),
        decode(Signature, signature),
        purpose=PURPOSE,
    )
    require(
        approval
        == approval_payload(**{
            'sha256': approval.get('source_sha256'),
            'service': approval.get('target_service'),
            **{
                key: approval.get(key)
                for key in (
                    'parent',
                    'parent_generation',
                    'operator',
                    'identities',
                    'expires_at',
                )
            },
        }),
        'legacy_approval_invalid',
    )
    require(approval['target_service'] == app.settings.service_url, 'legacy_service_mismatch')
    now = app.clock()
    expiry = parse_time(approval['expires_at'])
    require(now < expiry <= now + timedelta(hours=24), 'legacy_approval_expired')
    sha256 = approval['source_sha256']
    manifest, boards, posts, attachments, tags = content_plan(Path(snapshot), sha256)
    identities = approval['identities']
    require(isinstance(identities, dict), 'legacy_identity_mapping_invalid')
    required = {row.get('author_id') or '__anonymous__' for _, row in posts}
    require(required == set(identities), 'legacy_identity_mapping_incomplete')
    board_names = {row['name'] for row in boards}
    require(all(row['board'] in board_names for _, row in posts), 'legacy_board_missing')
    keys = {(table, row['id']) for table, row in posts}
    require(
        all(
            (('posts' if table == 'attachments' else 'archived_posts'), row['post_id']) in keys
            for table, row in attachments
        ),
        'legacy_attachment_post_missing',
    )
    prefix = 'legacy_' + sha256[:24]

    def resource_id(table, key):
        return prefix + '_' + digest((table, key))[7:31]

    destinations = {(table, row['id']): resource_id(table, row['id']) for table, row in posts}
    url_map = {}
    request = SimpleNamespace(operation='legacy.content_import', contract_version=1, arguments={})
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
                    operations=frozenset({'legacy.content_import@1'}),
                    constraints={},
                ),
            ),
        ),
    )
    async with app.metadata.transaction(write=True) as tx:
        require_live_authority(tx)
        operator = await tx.subject(approval['operator'])
        require(operator.kind == 'registered', 'legacy_operator_must_be_registered')
        for target in identities.values():
            require(target is None or isinstance(target, str), 'legacy_mapping_target_invalid')
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
        require(tx.setting('legacy-import:' + sha256) is None, 'legacy_already_imported')
        topics = {}
        for board in boards:
            try:
                topic_name = validate_name(board['name'])
            except Failure:
                # Legacy boards can use names reserved by v4 views, e.g. meta.
                topic_name = 'legacy-board-' + digest(board['name'])[7:31]
            topic = await create_resource(
                app,
                context,
                request,
                tx,
                parent=parent.id,
                type='topic',
                name=topic_name,
                mode=0o700,
                resource_id=resource_id('boards', board['name']),
                body=board['description'],
            )
            tx.execute(
                'INSERT INTO topic_settings(topic,membership_policy) VALUES (?,?)',
                (topic.id, 'closed'),
                write=True,
            )
            tx.execute(
                'INSERT INTO topic_memberships(topic,subject,role,status,joined_at) VALUES (?,?,?,?,?)',
                (topic.id, operator.resource_id, 'admin', 'active', wire(now)),
                write=True,
            )
            topics[board['name']] = topic.id
            url_map['/' + board['name']] = topic.id
        # Allocate all post resources before mapping replies and attachments.
        resources = {}
        for table, row in posts:
            resource = await create_resource(
                app,
                context,
                request,
                tx,
                parent=topics[row['board']],
                type='post',
                name=f'{table}-{row["id"]}.md',
                mode=0o600,
                resource_id=destinations[table, row['id']],
            )
            resources[table, row['id']] = resource
        # Installed find_in_board resolves BOTH global id and board sequence,
        # choosing the lowest global id on a collision. Reproduce that exactly.
        for table, row in sorted(posts, key=lambda item: item[1]['id']):
            for number in (row['id'], row['seq']):
                path = f'/{row["board"]}/{number}'
                if table == 'archived_posts':
                    path = '/_legacy/archive' + path
                for suffix in ('', '/raw', '/meta'):
                    url_map.setdefault(path + suffix, destinations[table, row['id']])
        attached = {}
        for table, row in attachments:
            key = ('posts' if table == 'attachments' else 'archived_posts', row['post_id'])
            target = resources[key]
            resource = await create_resource(
                app,
                context,
                request,
                tx,
                parent=target.parent,
                type='attachment',
                name=f'{table}-{row["id"]}',
                mode=0o600,
                body=row['data'],
                media_type=row['content_type'],
                resource_id=resource_id(table, row['id']),
            )
            attached.setdefault(key, []).append(
                Relation(
                    type='attachment',
                    target=ResourceRef(id=resource.id, revision=resource.revision),
                )
            )
            url_map[f'/_legacy/{table}/{row["id"]}'] = resource.id
            if table == 'attachments':
                url_map[f'/file/{row["id"]}'] = resource.id
                url_map[f'/file/{row["id"]}/meta'] = resource.id
        for table, row in posts:
            key = (table, row['id'])
            reply = row.get('reply_to')
            reply_target = None
            if reply is not None:
                candidates = [
                    value for (kind, old_id), value in destinations.items() if old_id == reply
                ]
                require(len(candidates) == 1, 'legacy_reply_mapping_ambiguous')
                reply_target = candidates[0]
            # Historical claims stay separate from the unsigned v4 import revision.
            allowed = (
                'id',
                'board',
                'seq',
                'name',
                'title',
                'created',
                'updated',
                'author_id',
                'author_key',
                'actor_id',
                'actor_key',
                'signature',
                'sig_version',
                'sig_nonce',
                'sig_issued',
                'reply_to',
                'deleted',
                'hidden',
                'system',
            )
            provenance = {
                'format': PURPOSE,
                'source_sha256': sha256,
                'table': table,
                'old': {field: row[field] for field in allowed if field in row},
                'historical_author_target': identities[row.get('author_id') or '__anonymous__'],
                'reply_target': reply_target,
                'legacy_signature_verified': False,
                'archived': table == 'archived_posts',
            }
            record = await create_resource(
                app,
                context,
                request,
                tx,
                parent=resources[key].parent,
                type='file',
                name=f'provenance-{table}-{row["id"]}.json',
                mode=0o600,
                body=canonical(provenance),
                media_type='application/json',
            )
            revision = await revise_resource(
                app,
                context,
                request,
                tx,
                resources[key],
                row['body'],
                relations=tuple(attached.get(key, ())),
                change_note='Legacy content import; original claims retained separately',
                source_kind='operation',
                source_version=1,
                source_digest='sha256:' + sha256,
            )
            tx.set_setting(
                'legacy-provenance:' + revision.id,
                {
                    'resource': record.id,
                    'reply_target': reply_target,
                    'source_table': table,
                    'source_id': row['id'],
                },
            )
            selected_tags = (
                tuple(sorted({tag for old_id, tag in tags if old_id == row['id']}))
                if table == 'posts'
                else ()
            )
            if selected_tags:
                await tx.replace(
                    replace(revision, tags=selected_tags, generation=revision.generation + 1),
                    revision.generation,
                )
        require(_digest(Path(snapshot)) == sha256, 'legacy_snapshot_changed')
        report = {
            'format': PURPOSE,
            'source_sha256': sha256,
            'parent': parent.id,
            'approval_digest': digest(approval),
            'boards': len(boards),
            'posts': len(posts),
            'attachments': len(attachments),
            'url_map': url_map,
            'authority_enabled': False,
            'external_jobs_enabled': False,
            'visibility': 'private',
            'source_table_counts': {
                name: data['count'] for name, data in manifest['tables'].items()
            },
        }
        tx.set_setting(
            'legacy-import-approval:' + sha256,
            {
                'approval': approval,
                'signature': signature,
                'root_public_key': b64(app.certificates.root_public_key),
            },
        )
        tx.set_setting('legacy-import:' + sha256, report)
        return report


def identity_requirements(snapshot: Path, sha256: str, identities: dict, *, summary_only=True):
    """Return missing/unexpected mapping keys without exposing bodies or key material."""
    _, _, posts, _, _ = content_plan(snapshot, sha256)
    required = {row.get('author_id') or '__anonymous__' for _, row in posts}
    if summary_only:
        return {
            'required_count': len(required),
            'missing_count': len(required - set(identities)),
            'unexpected_count': len(set(identities) - required),
            'requirements_digest': digest(sorted(required)),
        }
    return {
        'required': sorted(required),
        'missing': sorted(required - set(identities)),
        'unexpected': sorted(set(identities) - required),
    }


def main():
    import argparse
    import asyncio
    import json

    from msg.application import Application
    from msg.config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--signed-approval', type=Path, required=True)
    args = parser.parse_args()
    envelope = json.loads(args.signed_approval.read_text())

    async def run():
        app = Application(load_settings(args.config))
        try:
            await app.load()
            result = await import_content(
                app, args.snapshot, envelope['approval'], envelope['signature']
            )
            print(json.dumps(result, indent=2))
        finally:
            await app.close()

    asyncio.run(run())


if __name__ == '__main__':
    main()
