"""Authorized catalog reads shared by published checkout versions.

Version-specific schemas, creation and updates stay at catalog entrypoints.
"""
from __future__ import annotations
from msg.core.codec import loads
from msg.core.errors import Failure, require
from msg.core.models import ResourceRef
from msg.plugins.common import check_access, resolve
from msg.market.order_records import require_signed_subject


async def read_listing(app, ctx, request, tx, value, *, seller=False):
    try:
        rid = await resolve(tx, value)
        resource = await tx.resource(rid)
        require(resource.type == 'listing' and resource.parent == 't_store', 'listing_not_found')
        await check_access(app, ctx, request, tx, rid, 'read')
        if seller:
            require(resource.owner == require_signed_subject(ctx), 'listing_not_found')
        return resource
    except Failure as exc:
        if exc.code in {'not_found', 'permission_denied', 'credential_ceiling',
                        'certificate_gate', 'ancestor_inactive'}:
            raise Failure('listing_not_found') from None
        raise


async def read_listing_body(app, tx, resource, revision=None):
    rev = await tx.revision(ResourceRef(id=resource.id, revision=revision))
    body = loads(await app.contents.read_bytes(rev.content))
    # Listings deposited before the sale/bounty split were all sale listings.
    return {'mode': 'sale', **body}, rev


async def read_package_record(tx, package_id):
    return tx.one('''SELECT id,listing_id,listing_revision,seller,revision,kind,manifest,
        payload_refs,digest,total_size,delivery_mode,deposited_at FROM store_packages WHERE id=?''',
        (package_id,))
