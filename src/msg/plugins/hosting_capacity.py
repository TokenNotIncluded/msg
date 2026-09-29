"""Concrete byte-capacity entitlement consumer; never an authorization source.

The quota is the logical size of current website deployments owned by a subject.
Expired capacity does not delete published content; it only limits future writes.
"""

from msg.core.codec import decode, loads, wire
from msg.core.errors import require
from msg.core.models import ResourceRef

ENTITLEMENT_KIND = 'hosting_bytes_v1'
PROVIDER_VERSION = 1
BASE_CAPACITY_BYTES = 10 * 1024 * 1024
MAX_CAPACITY_BYTES = 1024**4


def extra_capacity(tx, subject, now):
    return int(
        tx.one(
            """SELECT COALESCE(SUM(quantity),0) FROM resource_entitlements
        WHERE subject_id=? AND entitlement_kind=? AND
        (expires_at IS NULL OR expires_at>?)""",
            (subject, ENTITLEMENT_KIND, wire(now)),
        )[0]
    )


async def manifest_size(app, tx, resource, revision_id=None):
    revision_id = revision_id or resource.revision
    if revision_id is None:
        return 0
    revision = await tx.revision(ResourceRef(id=resource.id, revision=revision_id))
    value = loads(await app.contents.read_bytes(revision.content))
    require(
        isinstance(value, dict) and isinstance(value.get('entries'), dict),
        'invalid_hosting_manifest',
    )
    size = 0
    for ref in value['entries'].values():
        item = await tx.revision(decode(ResourceRef, ref))
        size += item.content.size
        require(size <= MAX_CAPACITY_BYTES, 'hosting_capacity_exceeded')
    return size


async def used_capacity(app, tx, owner, *, excluding=None):
    total = 0
    for (rid,) in tx.rows(
        "SELECT id FROM resources WHERE type='website' AND owner=? AND state='active'", (owner,)
    ):
        if rid != excluding:
            total += await manifest_size(app, tx, await tx.resource(rid))
    return total


async def require_capacity(app, tx, resource, proposed_size, now, *, owner=None):
    owner = owner or resource.owner
    used = await used_capacity(app, tx, owner, excluding=resource.id)
    capacity = app.settings.hosting_base_capacity_bytes + extra_capacity(tx, owner, now)
    require(used + proposed_size <= min(capacity, MAX_CAPACITY_BYTES), 'hosting_capacity_exceeded')
