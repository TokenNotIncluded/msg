"""Finite, versioned authority vocabulary. A ceiling is a limit, not a grant."""

from __future__ import annotations

from dataclasses import replace

from msg.constants import ROOT_SPACE
from msg.core.models import CapabilityGrant, CapabilitySpec, ResourceRef, Scope
from msg.plugins.schemas import NETWORK_CONSTRAINTS

# Base capabilities route ordinary actions to mode/ownership checks. They never
# stand in for a special capability or a trusted local execution entry.
BASE_FAMILIES = {
    'identity.basic': (
        'identity.rename',
        'identity.register',
        'identity.temporary',
        'identity.custodial_create',
        'identity.upgrade',
        'identity.token_rotate',
        'identity.token_create',
        'identity.oauth_request',
        'identity.oauth_approve',
        'identity.token_recover',
        'identity.upgrade_result',
        'identity.key_add',
        'identity.key_revoke',
        'identity.email_set',
        'identity.email_verify',
        'identity.email_get',
        'identity.email_notifications',
        'identity.delegate',
        'identity.delegated_create',
        'identity.delegated_get',
        'identity.delegation_revoke',
        'identity.link_open',
        'identity.link_pending',
        'identity.link_release',
        'identity.link_close',
        'identity.certificate_renew',
        'identity.ssh_key_add',
        'identity.ssh_key_revoke',
        'identity.ssh_certificates',
        'identity.identity_key_get',
        'identity.identity_key_list',
        'identity.encryption_key_get',
        'identity.encryption_key_list',
        'identity.encryption_key_rotate',
        'identity.custodial_status',
        'identity.custodial_upgrade_start',
        'identity.custodial_upgrade_finish',
        'identity.personal_put',
        'identity.soul_visibility',
        'identity.note_put',
        'identity.custodial_rewrap_entry',
        'identity.custodial_rewrap_revision',
        'identity.custodial_rewrap_ack',
        'identity.custodial_migration_get',
        'identity.custodial_upgrade_result',
        'identity.custodial_upgrade_inventory',
        'identity.note_list',
        'identity.note_get',
        'identity.note_archive',
        'identity.note_restore',
        'identity.todo_put',
        'identity.todo_list',
        'identity.todo_get',
        'identity.todo_archive',
        'identity.todo_restore',
        'identity.legacy_put',
        'identity.legacy_archive',
        'identity.legacy_get',
        'identity.legacy_status',
        'identity.recovery_policy_get',
        'identity.recovery_policy_set',
        'identity.recovery_envelope_register',
        'identity.recovery_envelope_get',
        'identity.recovery_envelope_list',
        'identity.recovery_custodians',
        'achievement.start',
        'achievement.answer',
        'achievement.finish',
        'achievement.pin',
        'achievement.unpin',
        'achievement.reorder',
    ),
    'resource.basic': ('content.', 'file.'),
    'sharing.basic': ('sharing.',),
    'discussion.basic': ('discussion.',),
    'communication.basic': ('communication.',),
    'money.basic': ('money.',),
    'store.basic': ('store.',),
    'bounty.basic': ('bounty.',),
    'orders.basic': ('orders.',),
    'delivery.basic': ('delivery.',),
    'discovery.basic': ('discovery.', 'cert.get', 'job.get', 'achievement.list'),
    'transfer.basic': ('transfer.', 'query.'),
    'group.basic': ('group.',),
    'cert.request': ('cert.request', 'cert.cancel'),
    'git.basic': ('git.',),
    'hosting.basic': ('hosting.',),
    'keystore.basic': ('keystore.',),
    'batch.basic': ('batch.',),
}
EXCLUDED_BASE = {'content.purge', 'content.chown', 'identity.recover'}
TEMPORARY_OPERATIONS = frozenset({
    'identity.rename',
    'identity.oauth_request',
    'identity.oauth_approve',
    'identity.temporary',
    'identity.custodial_create',
    'identity.upgrade',
    'identity.token_rotate',
    'identity.custodial_status',
    'identity.custodial_upgrade_start',
    'identity.custodial_upgrade_finish',
    'identity.custodial_rewrap_entry',
    'identity.custodial_rewrap_revision',
    'identity.custodial_migration_get',
    'identity.custodial_upgrade_inventory',
    'identity.custodial_rewrap_ack',
    'content.topic_create',
    'content.public_board_update',
    'content.post_create',
    'content.post_edit',
    'content.archive',
    'content.restore',
    'content.file_put',
    'content.attach',
    'discussion.reply',
    'discussion.quote',
    'discussion.repost',
    'discussion.like',
    'discussion.unlike',
    'discussion.ack',
    'discussion.acks',
    'discussion.reading_manifest',
    'discussion.reading_prove',
    'discussion.readings',
    'discussion.thread',
    'communication.send',
    'communication.watch',
    'communication.unwatch',
    'communication.inbox',
    'communication.outbox',
    'communication.changes',
    'communication.events',
    'communication.event',
    'communication.handoff_create',
    'communication.handoff_get',
    'communication.handoff_list',
    'communication.handoff_decide',
    'communication.lease_acquire',
    'communication.lease_get',
    'communication.lease_list',
    'communication.lease_renew',
    'communication.lease_release',
    'discovery.get',
    'discovery.resolve',
    'discovery.raw',
    'discovery.list',
    'discovery.search',
    'discovery.operations',
    'discovery.capabilities',
    'discovery.schema',
    'discovery.diff',
    'discovery.references',
    'transfer.open',
    'transfer.part_put',
    'transfer.part_get',
    'transfer.status',
    'transfer.seal',
    'transfer.cancel',
    'keystore.get',
    'batch.independent',
    'batch.atomic',
    'job.get',
    'achievement.start',
    'achievement.answer',
    'achievement.finish',
    'achievement.list',
    'achievement.pin',
    'achievement.unpin',
    'achievement.reorder',
})


def _selected(operations, selectors):
    return frozenset(
        f'{op.name}@{op.version}'
        for op in operations
        if any(op.name.startswith(s) if s.endswith('.') else op.name == s for s in selectors)
    )


def install_capabilities(registry):
    operations = registry.operations()
    types = frozenset(spec.name for spec in registry.resource_types())
    {op.name: op for op in operations}
    network_constraints = ResourceRef(id='schema:network-constraints')
    registry.add_schema(network_constraints, NETWORK_CONSTRAINTS)
    for name, selectors in BASE_FAMILIES.items():
        names = _selected(operations, selectors) - _selected(operations, EXCLUDED_BASE)
        if names:
            registry.add_capability(
                CapabilitySpec(
                    name=name,
                    version=1,
                    scope_types=types,
                    operations=names,
                    replaces_checks=frozenset(),
                    delegatable=True,
                    ca_only=False,
                    constraints_schema=None,
                )
            )
    reads = frozenset(f'{op.name}@{op.version}' for op in operations if op.effect == 'read')
    writes = frozenset(f'{op.name}@{op.version}' for op in operations if op.effect != 'read')
    all_ops = reads | writes
    # An override only replaces the named check; it cannot waive a credential
    # ceiling, certgate, tool gate, sticky, or root.local_only.
    rules = {
        'resource.certified_write': (writes, ()),
        'resource.read_override': (all_ops, ('read', 'list', 'traverse')),
        'resource.write_override': (writes, ('write',)),
        'resource.chmod_override': (_selected(operations, ('content.chmod',)), ('chmod',)),
        'resource.chgrp_override': (_selected(operations, ('content.chgrp',)), ('chgrp',)),
        'resource.chown': (_selected(operations, ('content.chown',)), ('chown',)),
        'resource.purge': (_selected(operations, ('content.purge',)), ('purge',)),
        'identity.recover': (_selected(operations, ('identity.recover',)), ()),
        'group.manage_override': (
            _selected(
                operations,
                (
                    'group.member.',
                    'group.admin.',
                    'group.invite',
                    'group.approve',
                    'group.reject',
                    'group.remove',
                ),
            ),
            ('manage',),
        ),
        'tool.use': (
            _selected(
                operations,
                (
                    'tool.run',
                    'discovery.get',
                    'discovery.list',
                    'transfer.open',
                    'transfer.part_get',
                    'transfer.status',
                    'transfer.seal',
                    'transfer.cancel',
                    'discovery.raw',
                    'job.get',
                ),
            ),
            ('tool_use',),
        ),
        'tool.net.private': (_selected(operations, ('tool.run',)), ()),
        'webhook.domain': (_selected(operations, ('communication.webhook_subscribe',)), ()),
        'system.namespace': (all_ops, ()),
        'cert.issue': (_selected(operations, ('cert.publish', 'cert.get', 'discovery.get')), ()),
        'cert.revoke': (_selected(operations, ('cert.revoke',)), ()),
        'cert.ca.issue': (_selected(operations, ('cert.publish',)), ()),
        'system.inspect': (_selected(operations, ('system.inspect',)), ()),
        'system.config': (_selected(operations, ('system.config', 'system.share_links_set')), ()),
        'system.maintenance': (_selected(operations, ('system.maintenance',)), ()),
    }
    for name, (names, replaces) in rules.items():
        registry.add_capability(
            CapabilitySpec(
                name=name,
                version=1,
                scope_types=types,
                operations=names,
                replaces_checks=frozenset(replaces),
                delegatable=name not in {'cert.ca.issue'},
                ca_only=name.startswith('cert.'),
                constraints_schema=network_constraints if name.startswith('tool.') else None,
            )
        )
    # Publish a finite supported operation vocabulary on every resource type.
    # Handlers still check the precise role of each resource argument (source,
    # destination, owner, etc.), rather than inferring it from this directory.
    for key, spec in tuple(registry._types.items()):
        relations = (
            frozenset({
                'reply_to',
                'thread_root',
                'fork_of',
                'quote',
                'repost',
                'attachment',
                'template',
            })
            if spec.name == 'post'
            else frozenset()
        )
        if spec.name in {'collab_request', 'collab_offer', 'checkpoint', 'collab_proposal'}:
            relations = spec.relations
        registry._types[key] = replace(spec, operations=all_ops, relations=relations)


def grant_for(spec, *, scope=None, operations=None):
    return CapabilityGrant(
        capability=spec.name,
        version=spec.version,
        scope=scope or Scope(resource_id=ROOT_SPACE, descendants=True),
        operations=spec.operations if operations is None else frozenset(operations),
        constraints={},
    )


def primary_ceiling(registry, scope=None):
    # Explicit snapshot: future registry entries do not enter existing credentials.
    eligible = credential_operations(registry)
    return tuple(
        grant_for(spec, scope=scope, operations=spec.operations & eligible)
        for spec in registry.capabilities()
        if spec.operations & eligible
    )


def credential_operations(registry):
    # Declarations remain stable so historical signed snapshots still validate.
    # Only enabled authenticated operations enter newly requested ceilings.
    return frozenset(
        f'{spec.name}@{spec.version}'
        for spec in registry.operations()
        if spec.enabled and not spec.anonymous_only
    )


def base_grants(registry, scope=None):
    eligible = credential_operations(registry)
    return tuple(
        grant_for(spec, scope=scope, operations=spec.operations & eligible)
        for spec in registry.capabilities()
        if spec.name in BASE_FAMILIES and spec.operations & eligible
    )


def temporary_ceiling(registry, scope=None):
    temporary = frozenset(f'{name}@1' for name in TEMPORARY_OPERATIONS) | {
        'identity.upgrade@2',
        'identity.temporary@2',
        'identity.temporary@3',
        'identity.custodial_create@2',
        'identity.custodial_upgrade_finish@2',
        'identity.custodial_rewrap_ack@2',
        'identity.token_rotate@2',
    }
    temporary &= credential_operations(registry)
    return tuple(
        grant_for(spec, scope=scope, operations=spec.operations & temporary)
        for spec in registry.capabilities()
        if spec.name in BASE_FAMILIES and spec.operations & temporary
    )
