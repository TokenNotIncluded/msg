"""Exercise complete resource authorization against Python, using public fixtures.

The helper authenticates every request before authorization. It never accepts a
serialized Principal, and no test publishes data or reaches a production server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from test_identity_parity import NOW, READ, WRITE, Check, Fixture

from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.core.models import AccessRequirement, ResourceTypeSpec
from msg.security.authorization import AuthorizationService
from msg.security.capabilities import BASE_FAMILIES

EXTRA = frozenset({
    'communication.dm_send@1',
    'communication.dm_send@2',
    'query.save@1',
    'query.saved_archive@1',
    'sharing.link_create@1',
    'sharing.grant@2',
    'sharing.revoke@2',
    'identity.note_put@2',
    'identity.personal_put@2',
    'identity.todo_put@2',
    'identity.legacy_put@2',
    'content.archive@1',
    'content.move@1',
    'content.chmod@1',
    'content.topic_create@1',
    'content.file_put@1',
    'wiki.page_put@1',
})


class PolicyFixture(Fixture):
    def __init__(self, path):
        super().__init__(path)
        # Trusted test configuration, not part of a request. Use the real names
        # of the built-in families; no synthetic bypass to ordinary checks.
        self.primary = tuple(replace(g, operations=g.operations | EXTRA) for g in self.primary)
        for key, spec in list(self.registry._capabilities.items()):
            self.registry._capabilities[key] = replace(spec, operations=spec.operations | EXTRA)
        for name, cred in self.credentials.items():
            cred = replace(cred, ceiling=self.primary)
            self.credentials[name] = cred
            self.conn.execute(
                'UPDATE credentials SET body=? WHERE id=?', (canonical(cred).decode(), cred.id)
            )
        for kind in (
            'space',
            'topic',
            'file',
            'post',
            'user',
            'organization',
            'tool',
            'csr',
            'certificate',
            'credential',
            'legacy_directive',
            'dm_conversation',
            'watch',
            'saved_query',
            'delegation',
            'todo',
        ):
            self.registry.add_resource_type(
                ResourceTypeSpec(
                    name=kind,
                    version=1,
                    container=kind in {'space', 'topic', 'user', 'organization', 'dm_conversation'},
                    content_schema=None,
                    operations=frozenset(),
                    relations=frozenset(),
                )
            )
        self.policy = AuthorizationService(self.registry, self.validator)
        self.put_resource('r_user', parent='r_root', kind='user', owner='alice')
        self.put_resource('r_soul', parent='r_user', name='SOUL.md', owner='alice')
        self.put_resource('r_notes', parent='r_user', name='notes', kind='topic', owner='alice')
        self.put_resource('r_note', parent='r_notes', name='note.md', owner='alice')
        self.put_resource('r_todos', parent='r_user', name='todos', kind='topic', owner='alice')
        self.put_resource('r_todo', parent='r_todos', kind='todo', owner='alice')
        self.put_resource('t_last_will', parent='r_root', kind='topic')
        self.put_resource('r_legacy', parent='t_last_will', kind='legacy_directive', owner='alice')
        self.put_resource('t_wiki', parent='r_root', kind='topic')
        self.put_resource('r_wiki', parent='t_wiki', owner='alice')
        self.put_resource('r_agents', parent='r_root', owner='alice')
        self.put_resource('r_rules', parent='r_root', owner='alice')
        self.put_resource('r_watch', parent='r_area', kind='watch', owner='alice')
        self.put_resource('r_query', parent='r_area', kind='saved_query', owner='alice')
        self.put_resource('r_about', parent='r_area', name='ABOUT.md', owner='alice')
        self.put_resource('r_header', parent='r_area', name='HEADER.svg', owner='alice')
        self.put_resource('t_tools', parent='r_root', kind='topic', mode=0o700)
        self.put_resource('r_tool', parent='t_tools', kind='tool', mode=0o700)
        self.put_resource('r_dm', parent='r_root', kind='topic', mode=0o777)
        self.put_resource('r_message', parent='r_dm', kind='post', owner='alice', mode=0o777)
        self.put_resource('g_test', parent='r_root', kind='organization')
        self.conn.execute(
            'INSERT INTO identities VALUES (?,?,?,?)',
            (
                'g_test',
                'organization',
                0,
                canonical({'resource_id': 'g_test', 'membership_version': 1}).decode(),
            ),
        )
        for member in ('alice', 'bob'):
            self.conn.execute(
                'INSERT INTO memberships VALUES (?,?,?,?)',
                (
                    'g_test',
                    self.users[member],
                    1,
                    canonical({
                        'organization_id': 'g_test',
                        'subject_id': self.users[member],
                        'role': 'member',
                        'version': 1,
                    }).decode(),
                ),
            )
        self.conn.execute(
            """INSERT INTO dm_conversations
            (pair,resource_id,participant_a,participant_b,initiator,conversation_kind,state,created_at,updated_at)
            VALUES (?,?,?,?,?,'direct','active',?,?)""",
            (
                'pair_fixture',
                'r_dm',
                *sorted((self.users['alice'], self.users['bob'])),
                self.users['alice'],
                wire(NOW),
                wire(NOW),
            ),
        )

    def put_resource(self, id, *, parent='r_area', kind='file', name=None, owner='bob', mode=0o755):
        resource = replace(
            self.resources['r_file'],
            id=id,
            parent=parent,
            type=kind,
            name=name or id,
            owner=self.users.get(owner, owner),
            mode=mode,
            revision='v_' + id,
        )
        self.resources[id] = resource
        self.conn.execute(
            'INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                id,
                kind,
                resource.name,
                parent,
                resource.owner,
                resource.group,
                mode,
                0,
                resource.revision,
                'active',
                wire(NOW),
                wire(NOW),
                canonical(resource).decode(),
            ),
        )
        return resource

    def context(self):
        return {
            **super().context(),
            'resource_types': wire(self.registry.resource_types()),
            'base_families': list(BASE_FAMILIES),
        }

    @contextmanager
    def resource_change(self, id, **fields):
        with self.body_change('resources', id, replace(self.resources[id], **fields)):
            yield

    @contextmanager
    def row_change(self, table, key, updates):
        # Table, columns, and predicates are test-owned constants, not request data.
        where = ' AND '.join(f'{k}=?' for k in key)
        columns = ','.join(updates)
        old = self.conn.execute(
            f'SELECT {columns} FROM {table} WHERE {where}', tuple(key.values())
        ).fetchone()
        assert old is not None
        assign = ','.join(f'{k}=?' for k in updates)
        self.conn.execute(
            f'UPDATE {table} SET {assign} WHERE {where}', (*updates.values(), *key.values())
        )
        try:
            yield
        finally:
            self.conn.execute(f'UPDATE {table} SET {assign} WHERE {where}', (*old, *key.values()))


class PolicyCheck(Check):
    async def require_case(
        self,
        group,
        *,
        id='r_file',
        check='read',
        actor='alice',
        operation=READ + '@1',
        certificates=(),
        name=None,
        checks=None,
        entry='network',
        delegated=False,
    ):
        f = self.fixture
        opname, version = operation.rsplit('@', 1)
        policy = f.spec(opname, version=int(version), entries=('network', 'local_admin', 'worker'))
        request = f.request(
            name=opname,
            version=int(version),
            actor=actor,
            anonymous=actor is None,
            subject=None if actor is None else f.users['bob'] if delegated else 'default',
            certificates=certificates,
            arguments={} if name is None else {'name': name},
        )
        requirements = checks or [
            AccessRequirement(resource_id=id, operation=operation, check=check)
        ]
        async with f.metadata.transaction(write=False) as tx:
            try:
                principal = await f.auth.authenticate(request, tx, entry=entry)
                context = SimpleNamespace(principal=principal, now=f.now, entry=entry)
                refs = await f.policy.require(context, request, requirements, tx)
                expected = {'ok': True, 'data': {'refs': wire(refs)}}
            except Failure as exc:
                expected = {'ok': False, 'code': exc.code}
        actual = self.native({
            'action': 'authorize',
            'raw': canonical(request).decode(),
            'policy': policy,
            'entry': entry,
            'now': wire(f.now),
            'checks': wire(requirements),
            'request_name': name,
        })
        self.compare(group, actual, expected)
        return actual

    async def source_case(self, group, id='r_file', grant='s_main', subject='alice', reshare=False):
        f = self.fixture
        async with f.metadata.transaction(write=False) as tx:
            try:
                resource = await tx.resource(id)
                result = await f.policy.share_source_active(
                    resource, grant, f.users.get(subject, subject), f.now, tx, reshare=reshare
                )
                expected = {'ok': True, 'data': {'allowed': result}}
            except Failure as exc:
                expected = {'ok': False, 'code': exc.code}
        self.compare(
            group,
            self.native({
                'action': 'share_source',
                'id': id,
                'grant': grant,
                'subject': f.users.get(subject, subject),
                'now': wire(f.now),
                'reshare': reshare,
            }),
            expected,
        )

    async def link_case(self, group, id='r_file', grantor='bob', credential=None):
        f = self.fixture
        credential = credential or f.keys[grantor]
        async with f.metadata.transaction(write=False) as tx:
            try:
                r = await tx.resource(id)
                result = await f.policy.share_link_read_allowed(
                    f.users[grantor], credential, r, (*await tx.ancestors(id), r), f.now, tx
                )
                expected = {'ok': True, 'data': {'allowed': result}}
            except Failure as exc:
                expected = {'ok': False, 'code': exc.code}
        self.compare(
            group,
            self.native({
                'action': 'share_link',
                'id': id,
                'grantor': f.users[grantor],
                'credential': credential,
                'now': wire(f.now),
            }),
            expected,
        )


def insert_share(
    f,
    id='s_main',
    *,
    resource='r_file',
    grantor='bob',
    grantee='alice',
    parent=None,
    kind='user',
    reshare=True,
):
    f.conn.execute(
        """INSERT INTO share_grants_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            id,
            resource,
            f.users.get(grantor, grantor),
            f.users.get(grantee, grantee),
            kind,
            parent,
            '["read"]',
            '{}',
            int(reshare),
            wire(NOW - timedelta(minutes=1)),
            wire(NOW + timedelta(hours=1)),
            None,
        ),
    )


async def exercise(f, c):
    for actor in ('alice', 'bob', None):
        for mode in (
            0o000,
            0o100,
            0o400,
            0o600,
            0o640,
            0o700,
            0o750,
            0o755,
            0o777,
            0o1777,
            0o2777,
            0o4777,
        ):
            with f.resource_change('r_file', mode=mode):
                for check in (
                    'read',
                    'list',
                    'traverse',
                    'write',
                    'create',
                    'remove',
                    'chmod',
                    'chgrp',
                    'chown',
                    'manage',
                    'certgate',
                    'purge',
                    'tool_use',
                ):
                    await c.require_case('resource_policy', actor=actor, check=check)
    for group_kind in ('active', 'pending', 'invited', 'rejected'):
        body = {
            'organization_id': 'g_test',
            'subject_id': f.users['alice'],
            'role': 'member',
            'version': 1,
            'status': group_kind,
        }
        with f.row_change(
            'memberships',
            {'org': 'g_test', 'subject': f.users['alice']},
            {'body': canonical(body).decode()},
        ):
            async with f.metadata.transaction(write=False) as tx:
                expected = {
                    'ok': True,
                    'data': {
                        'groups': sorted(
                            m.organization_id for m in await tx.memberships(f.users['alice'])
                        )
                    },
                }
            c.compare(
                'memberships',
                c.native({'action': 'memberships', 'subject': f.users['alice']}),
                expected,
            )
            with f.resource_change('r_file', mode=0o040):
                await c.require_case('group_mode')
    for parentmode in (0, 0o100, 0o400, 0o700, 0o710, 0o755):
        with f.resource_change('r_area', mode=parentmode):
            for actor in ('alice', 'bob', None):
                await c.require_case('ancestor_traversal', actor=actor)
    for state in ('archived', 'purged'):
        with f.resource_change('r_area', state=state):
            await c.require_case('inactive_ancestor')
    for id in (
        'r_watch',
        'r_query',
        'r_soul',
        'r_note',
        'r_todo',
        'r_legacy',
        'r_wiki',
        'r_agents',
        'r_rules',
    ):
        for actor in ('alice', 'bob', None):
            for check in ('read', 'write', 'manage'):
                await c.require_case('managed_resources', id=id, actor=actor, check=check)
    for id, operations in {
        'r_query': ('query.save@1', 'query.saved_archive@1'),
        'r_soul': ('identity.personal_put@2', 'sharing.grant@2'),
        'r_note': ('identity.note_put@2', 'sharing.grant@2'),
        'r_todo': ('identity.todo_put@2', 'sharing.grant@2'),
        'r_legacy': ('identity.legacy_put@2', 'content.archive@1'),
        'r_wiki': ('content.archive@1', 'content.move@1', 'content.chmod@1', 'wiki.page_put@1'),
    }.items():
        for op in operations:
            for actor in ('alice', 'bob'):
                await c.require_case(
                    'dedicated_operations', id=id, operation=op, actor=actor, check='write'
                )
    for resource in ('r_about', 'r_header', 'r_area'):
        for actor in ('alice', 'bob', None):
            for check in ('read', 'write', 'create', 'manage'):
                await c.require_case(
                    'topic_presentation', id=resource, actor=actor, check=check, name='ABOUT.md'
                )
    f.conn.execute(
        """INSERT INTO topic_memberships(topic,subject,role,status,joined_at,invited_by)
        VALUES (?,?,'admin','active',?,?)""",
        ('r_area', f.users['alice'], wire(NOW), None),
    )
    for id in ('r_about', 'r_header', 'r_area'):
        await c.require_case('topic_admin', id=id, check='write')
    for expires in (
        None,
        wire(NOW - timedelta(seconds=1)),
        wire(NOW),
        wire(NOW + timedelta(seconds=1)),
    ):
        f.conn.execute(
            """INSERT INTO topic_bans(topic,subject,actor,created_at,expires_at,reason,status)
            VALUES (?,?,?,?,?,'test','active') ON CONFLICT(topic,subject) DO UPDATE SET expires_at=excluded.expires_at""",
            ('r_area', f.users['alice'], f.users['bob'], wire(NOW), expires),
        )
        await c.require_case('topic_bans', id='r_about', check='write')
    f.conn.execute('DELETE FROM topic_bans')
    for prefix, id in (('hosting_preview:', 'r_area'), ('hosting_preview_file:', 'r_file')):
        for marker in (False, {}, {'owner': f.users['alice']}, {'owner': f.users['bob']}):
            with f.setting_change(prefix + id, marker):
                for actor in ('alice', 'bob', None):
                    await c.require_case('hosting_preview', actor=actor)
    for actor in ('alice', 'bob', 'ca', None):
        for check in ('read', 'list', 'traverse', 'write', 'create', 'manage', 'chmod'):
            await c.require_case('dm_participants', id='r_dm', actor=actor, check=check)
            await c.require_case('dm_messages', id='r_message', actor=actor, check=check)
    for state in ('active', 'pending', 'rejected'):
        with f.row_change('dm_conversations', {'resource_id': 'r_dm'}, {'state': state}):
            for actor in ('alice', 'bob'):
                await c.require_case(
                    'dm_send',
                    id='r_dm',
                    actor=actor,
                    check='create',
                    operation='communication.dm_send@2',
                )
                await c.require_case(
                    'dm_edit', id='r_message', actor=actor, check='write', operation=WRITE + '@1'
                )
    f.conn.execute('INSERT INTO dm_blocks VALUES (?,?)', (f.users['alice'], f.users['bob']))
    for actor in ('alice', 'bob'):
        await c.require_case(
            'dm_blocked',
            id='r_dm',
            actor=actor,
            check='create',
            operation='communication.dm_send@2',
        )
        await c.require_case(
            'dm_blocked', id='r_message', actor=actor, check='write', operation=WRITE + '@1'
        )
    f.conn.execute('DELETE FROM dm_blocks')
    for mode in (0o2000, 0o2777, 0o6755):
        with f.resource_change('r_area', mode=mode):
            for check in ('read', 'write', 'create', 'manage'):
                await c.require_case('certificate_gate', id='r_file', check=check, actor='bob')
    for actor in ('alice', 'bob', None):
        for check in ('read', 'tool_use', 'write'):
            await c.require_case('tool_gate', id='r_tool', check=check, actor=actor)
    for certificates in (('c_leaf',), ('c_online_leaf',), ('c_delegation',)):
        await c.require_case('attached_certificates', certificates=certificates)
    # A share authorizes one read, never traversal, writes, or descendant reads.
    insert_share(f)
    with f.resource_change('r_file', mode=0o600), f.resource_change('r_area', mode=0o700):
        for actor in ('alice', 'bob', 'ca', None):
            for check in ('read', 'list', 'traverse', 'write'):
                await c.require_case('share_exact_read', actor=actor, check=check)
    for field, values in {
        'resource_id': ('r_file', 'r_other'),
        'grantor': (f.users['bob'], f.users['alice']),
        'grantee': (f.users['alice'], f.users['bob']),
        'grantee_kind': ('user', 'group'),
        'operations': ('["read"]', '["read","write"]', '[]', 'null', '{bad', '["read","read"]'),
        'constraints': ('{}', '{"name":"secret"}', '[]', 'null', 'bad'),
        'expires_at': (
            wire(NOW),
            wire(NOW - timedelta(seconds=1)),
            wire(NOW + timedelta(seconds=1)),
            'invalid',
        ),
        'created_at': (
            wire(NOW),
            wire(NOW + timedelta(seconds=1)),
            wire(NOW - timedelta(days=1)),
            'invalid',
        ),
        'revoked_at': (None, wire(NOW)),
        'allow_reshare': (0, 1),
    }.items():
        for value in values:
            with f.row_change('share_grants_v2', {'id': 's_main'}, {field: value}):
                await c.source_case('share_facts')
                await c.source_case('share_reshare', reshare=True)
    with f.row_change(
        'share_grants_v2', {'id': 's_main'}, {'grantee': 'g_test', 'grantee_kind': 'group'}
    ):
        for subject in ('alice', 'bob', 'ca'):
            await c.source_case('group_share', subject=subject)
    insert_share(f, 's_child', grantor='alice', grantee='ca', parent='s_main')
    await c.source_case('share_chain', grant='s_child', subject='ca')
    for change in (
        {'revoked_at': wire(NOW)},
        {'allow_reshare': 0},
        {'expires_at': wire(NOW + timedelta(minutes=2))},
        {'parent_id': 's_child'},
    ):
        with f.row_change('share_grants_v2', {'id': 's_main'}, change):
            await c.source_case('share_chain', grant='s_child', subject='ca')
    for id in (
        'r_dm',
        'r_message',
        'r_soul',
        'r_todo',
        'r_legacy',
        'r_agents',
        'r_rules',
        'r_area',
        'r_tool',
        'c_leaf',
    ):
        with f.row_change('share_grants_v2', {'id': 's_main'}, {'resource_id': id}):
            await c.source_case('protected_share', id=id)
    for grantor in ('alice', 'bob'):
        await c.link_case('share_link', grantor=grantor)
    for change in (
        {'revoked_at': NOW},
        {'not_before': NOW + timedelta(seconds=1)},
        {'expires_at': NOW},
        {'ceiling': ()},
    ):
        with f.body_change('credentials', f.keys['bob'], replace(f.credentials['bob'], **change)):
            await c.link_case('share_link_credential')
    for key, value in (
        ('recovery_quarantine', False),
        ('recovery_quarantine', None),
        ('recovery_quarantine', {}),
    ):
        with f.setting_change(key, value):
            await c.require_case('quarantine', actor=None)
            await c.link_case('quarantined_share')
    # Once either authority service observed a replaced generation it remains
    # fenced even if a later restore reinstates the old database value.
    with f.setting_change('recovery_runtime_generation', 'replacement'):
        await c.require_case('runtime_generation')
    await c.require_case('runtime_generation_latched')


async def run(binary, report, postgres=False):
    from contextlib import nullcontext

    from postgres_fixture import database, replace_fixture_storage

    with tempfile.TemporaryDirectory(prefix='msg-rust-policy-') as temp:
        f = PolicyFixture(Path(temp) / 'metadata.sqlite3')
        with database() if postgres else nullcontext(None) as dsn:
            c = None
            try:
                if dsn:
                    await replace_fixture_storage(f, dsn)
                c = PolicyCheck(binary, f)
                await exercise(f, c)
                data = {
                    'backend': 'postgresql' if postgres else 'sqlite',
                    'cases': sum(c.groups.values()),
                    'groups': dict(c.groups),
                    'failures': c.failures,
                }
                report.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
                print(
                    json.dumps({
                        'cases': data['cases'],
                        'failures': len(c.failures),
                        'report': str(report),
                    })
                )
                if c.failures:
                    print(json.dumps(c.failures[:8], indent=2))
                return bool(c.failures)
            finally:
                if c is not None:
                    c.process.stdin.close()
                    c.process.wait(timeout=10)
                await f.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--postgres', action='store_true')
    options = parser.parse_args()
    raise SystemExit(
        asyncio.run(run(options.binary.resolve(), options.report.resolve(), options.postgres))
    )
