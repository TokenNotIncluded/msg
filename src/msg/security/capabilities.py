"""Finite, versioned authority vocabulary. A ceiling is a limit, not a grant."""
from __future__ import annotations

from dataclasses import replace
from msg.constants import ROOT_SPACE
from msg.core.models import CapabilitySpec, CapabilityGrant, Scope, ResourceRef
from msg.plugins.schemas import NETWORK_CONSTRAINTS

# Base capabilities route ordinary actions to mode/ownership checks. They never
# stand in for a special capability or a trusted local execution entry.
BASE_FAMILIES = {
    'identity.basic': ('identity.register', 'identity.temporary', 'identity.upgrade',
        'identity.token_rotate', 'identity.token_create', 'identity.key_add', 'identity.key_revoke',
        'identity.email_set', 'identity.email_verify', 'identity.email_get', 'identity.email_notifications',
        'identity.delegate', 'identity.delegation_revoke', 'identity.certificate_renew',
        'identity.ssh_key_add', 'identity.ssh_key_revoke', 'identity.ssh_certificates'),
    'resource.basic': ('content.',),
    'discussion.basic': ('discussion.',),
    'communication.basic': ('communication.',),
    'discovery.basic': ('discovery.', 'cert.get', 'job.get'),
    'transfer.basic': ('transfer.',),
    'group.basic': ('group.create', 'group.member.', 'group.admin.'),
    'cert.request': ('cert.request', 'cert.cancel'),
    'git.basic': ('git.',),
    'hosting.basic': ('hosting.',),
    'keystore.basic': ('keystore.',),
    'batch.basic': ('batch.',),
}
EXCLUDED_BASE = {'content.purge','content.chown','identity.recover'}
TEMPORARY_OPERATIONS = frozenset({
    'identity.temporary','identity.upgrade','identity.token_rotate',
    'content.topic_create','content.post_create','content.post_edit','content.archive',
    'content.restore','content.file_put','content.attach',
    'discussion.reply','discussion.quote','discussion.repost','discussion.like','discussion.unlike',
    'discussion.ack','discussion.acks','discussion.thread',
    'communication.send','communication.watch','communication.unwatch',
    'communication.inbox','communication.outbox','communication.changes',
    'discovery.get','discovery.raw','discovery.list','discovery.search','discovery.operations',
    'discovery.capabilities','discovery.schema','discovery.diff','discovery.references',
    'transfer.open','transfer.part_put','transfer.part_get','transfer.status','transfer.seal','transfer.cancel',
    'batch.independent','batch.atomic','job.get',
})


def _selected(operations, selectors):
    return frozenset(f'{op.name}@{op.version}' for op in operations
        if any(op.name.startswith(s) if s.endswith('.') else op.name==s for s in selectors))


def install_capabilities(registry):
    operations=registry.operations()
    types=frozenset(spec.name for spec in registry.resource_types())
    by_name={op.name:op for op in operations}
    network_constraints=ResourceRef(id='schema:network-constraints')
    registry.add_schema(network_constraints, NETWORK_CONSTRAINTS)
    for name,selectors in BASE_FAMILIES.items():
        names=_selected(operations,selectors)-frozenset(f'{n}@1' for n in EXCLUDED_BASE)
        if names:
            registry.add_capability(CapabilitySpec(name=name,version=1,scope_types=types,
                operations=names,replaces_checks=frozenset(),delegatable=True,ca_only=False,constraints_schema=None))
    reads=frozenset(f'{op.name}@{op.version}' for op in operations if op.effect=='read')
    writes=frozenset(f'{op.name}@{op.version}' for op in operations if op.effect!='read')
    all_ops=reads|writes
    # An override only replaces the named check; it cannot waive a credential
    # ceiling, certgate, tool gate, sticky, or root.local_only.
    rules={
        'resource.certified_write': (writes,()),
        'resource.read_override': (all_ops,('read','list','traverse')),
        'resource.write_override': (writes,('write',)),
        'resource.chmod_override': (_selected(operations,('content.chmod',)),('chmod',)),
        'resource.chgrp_override': (_selected(operations,('content.chgrp',)),('chgrp',)),
        'resource.chown': (_selected(operations,('content.chown',)),('chown',)),
        'resource.purge': (_selected(operations,('content.purge',)),('purge',)),
        'identity.recover': (_selected(operations,('identity.recover',)),()),
        'group.manage_override': (_selected(operations,('group.member.','group.admin.')),('manage',)),
        'tool.use': (_selected(operations,('tool.invoke','discovery.get','discovery.list','transfer.open',
                                          'transfer.part_get','transfer.status','transfer.seal','transfer.cancel','discovery.raw','job.get')),('tool_use',)),
        'tool.net.private': (_selected(operations,('tool.invoke',)),()),
        'system.namespace': (all_ops,()),
        'cert.issue': (_selected(operations,('cert.publish','cert.get','discovery.get')),()),
        'cert.revoke': (_selected(operations,('cert.revoke',)),()),
        'cert.ca.issue': (_selected(operations,('cert.publish',)),()),
        'system.inspect': (_selected(operations,('system.inspect',)),()),
        'system.config': (_selected(operations,('system.config',)),()),
        'system.maintenance': (_selected(operations,('system.maintenance',)),()),
    }
    for name,(names,replaces) in rules.items():
        registry.add_capability(CapabilitySpec(name=name,version=1,scope_types=types,operations=names,
            replaces_checks=frozenset(replaces),delegatable=name not in {'cert.ca.issue'},
            ca_only=name.startswith('cert.'),constraints_schema=network_constraints if name.startswith('tool.') else None))
    # Publish a finite supported operation vocabulary on every resource type.
    # Handlers still check the precise role of each resource argument (source,
    # destination, owner, etc.), rather than inferring it from this directory.
    for key,spec in tuple(registry._types.items()):
        relations=frozenset({'reply_to','thread_root','quote','repost','attachment','template'}) if spec.name=='post' else frozenset()
        registry._types[key]=replace(spec,operations=all_ops,relations=relations)


def grant_for(spec, *, scope=None, operations=None):
    return CapabilityGrant(capability=spec.name,version=spec.version,
        scope=scope or Scope(resource_id=ROOT_SPACE,descendants=True),
        operations=spec.operations if operations is None else frozenset(operations),constraints={})


def primary_ceiling(registry):
    # Explicit snapshot: future registry entries do not enter existing credentials.
    return tuple(grant_for(spec) for spec in registry.capabilities() if spec.operations)


def base_grants(registry):
    return tuple(grant_for(spec) for spec in registry.capabilities() if spec.name in BASE_FAMILIES and spec.operations)


def temporary_ceiling(registry):
    temporary=frozenset(f'{name}@1' for name in TEMPORARY_OPERATIONS)
    return tuple(grant_for(spec,operations=spec.operations&temporary) for spec in registry.capabilities()
        if spec.name in BASE_FAMILIES and spec.operations&temporary)
