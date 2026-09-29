"""Shared, current-state boundaries for grants, delegated sources and links.

Only the finite ShareGrant@2 contract (read, no constraints) is accepted. These
checks do not grant an owner, a role or a certificate any additional permission.
"""

from __future__ import annotations


def share_target_error(registry, resource, chain, session):
    """Return the same denial at issuance and at every source revalidation."""
    if resource.state != 'active' or any(item.state != 'active' for item in chain):
        return 'share_forbidden_resource'
    if registry.resource_type(resource.type, resource.type_version).container:
        return 'share_container_forbidden'
    if any(
        item.id in {'r_agents', 'r_rules', 't_last_will'}
        or item.type
        in {'tool', 'csr', 'certificate', 'credential', 'legacy_directive', 'dm_conversation'}
        for item in chain
    ):
        return 'share_forbidden_resource'
    if any(
        parent.type == 'user' and child.name in {'SOUL.md', 'AGENTS.md', 'todos'}
        for parent, child in zip(chain, chain[1:], strict=False)
    ):
        return 'share_forbidden_resource'
    if any(
        session.setting('hosting_preview:' + item.id)
        or session.setting('hosting_preview_file:' + item.id)
        for item in chain
    ):
        return 'share_forbidden_resource'
    if (
        session.one(
            'SELECT 1 FROM dm_conversations WHERE resource_id IN ('
            + ','.join('?' for _ in chain)
            + ') LIMIT 1',
            tuple(item.id for item in chain),
        )
        is not None
    ):
        return 'share_forbidden_resource'
    return None
