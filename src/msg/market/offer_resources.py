"""Local Root offer writer backed by signed ordinary Listing revisions.

Legacy rows without a mapping remain legacy; observing them never migrates them.
The SQL offer row is only a compatibility projection after a mapping is created.
"""
from dataclasses import replace

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import require
from msg.core.models import Resource, ResourceRef, Revision
from msg.security.crypto import verify

OFFER_COLUMNS = ('offer_id', 'resource_kind', 'unit', 'price_minor', 'min_quantity',
                 'max_quantity', 'entitlement_kind', 'duration_seconds', 'enabled',
                 'price_revision')


def mapped_listing(tx, offer_id):
    row = tx.one('SELECT listing_id FROM server_offer_resources WHERE offer_id=?', (offer_id,))
    return row[0] if row else None


def listing_body(offer):
    return {'mode': 'sale', 'item_kind': 'entitlement', 'price_minor': offer['price_minor'],
        'currency_id': 'primary', 'quantity': offer['max_quantity'],
        'delivery_mode': 'managed_instant', 'package_ref': None,
        'escrow_policy': 'legacy-entitlement-v1', 'dispute_policy': 'local-root-refund-v1',
        'terms': 'Server-issued entitlement; settled Root payments require local refund authority.',
        'expires_at': None, 'state': 'active' if offer['enabled'] else 'paused',
        'server_offer': dict(offer), 'server_offer_model': 1}


async def authoritative_offer(app, tx, offer_id):
    """Return the signed authority, or None for an unmigrated legacy row."""
    listing_id = mapped_listing(tx, offer_id)
    if listing_id is None:
        return None
    resource = await tx.resource(listing_id)
    require(resource.id == offer_id and resource.type == 'listing' and
            resource.parent == 't_store' and resource.owner == ROOT_SUBJECT and
            resource.state == 'active' and resource.mode == 0o444 and
            resource.group == 'g_public', 'offer_resource_invalid')
    ancestors = await tx.ancestors(resource.id)
    require(all(parent.state == 'active' and parent.mode & 0o004 for parent in ancestors),
            'offer_resource_invalid')
    revision = await tx.revision(ResourceRef(id=listing_id, revision=resource.revision))
    require(revision.actor == revision.subject == ROOT_SUBJECT and revision.signature is not None,
            'offer_resource_invalid')
    manifest = {k: v for k, v in wire(revision).items() if k not in {'manifest_digest', 'signature'}}
    require(digest(manifest) == revision.manifest_digest, 'offer_resource_invalid')
    verify(app.certificates.root_public_key, canonical(manifest), revision.signature, purpose='revision')
    data = await app.contents.read_bytes(revision.content)
    require(digest(data) == revision.content.digest, 'offer_resource_invalid')
    body = loads(data)
    require(isinstance(body, dict) and body.get('server_offer_model') == 1 and
            isinstance(body.get('server_offer'), dict), 'offer_resource_invalid')
    offer = body['server_offer']
    require(set(offer) == set(OFFER_COLUMNS) and offer['offer_id'] == offer_id and
            body == listing_body(offer), 'offer_resource_invalid')
    return offer


async def verify_projection(app, tx, row):
    """Fail closed if a mapped compatibility row diverges from the signed source."""
    source = await authoritative_offer(app, tx, row[0])
    if source is not None:
        expected = tuple(source[k] for k in (
            'offer_id', 'resource_kind', 'unit', 'price_minor', 'min_quantity',
            'max_quantity', 'entitlement_kind', 'duration_seconds', 'price_revision'))
        require(tuple(row) == expected and source['enabled'], 'offer_projection_mismatch')
    return source


async def write_offer_revision(app, tx, signer, offer, *, now, request_id, new=False,
                               revision_id=None, source_digest=None):
    """Caller owns the local authorization and database transaction."""
    require(signer.public_key == app.certificates.root_public_key, 'root_key_mismatch')
    app.registry.resource_type('listing', 1)
    offer_id = offer['offer_id']
    if new:
        parent = await tx.resource('t_store')
        require(parent.state == 'active', 'ancestor_inactive')
        require(app.registry.resource_type(parent.type, parent.type_version).container, 'not_a_container')
        require(tx.one('SELECT 1 FROM resources WHERE id=? OR (parent=? AND name=?)',
                       (offer_id, 't_store', offer_id)) is None, 'offer_resource_conflict')
        resource = Resource(id=offer_id, type='listing', type_version=1, name=offer_id,
            parent='t_store', owner=ROOT_SUBJECT, group='g_public', mode=0o444,
            generation=0, revision=None, state='active', created_at=now, created_by=ROOT_SUBJECT,
            modified_at=now, modified_by=ROOT_SUBJECT)
        await tx.insert(resource)
    else:
        resource = await tx.resource(mapped_listing(tx, offer_id))
    # Disable has a new immutable revision but retains the published quote ID.
    from uuid import uuid4
    revision_id = revision_id or (offer['price_revision'] if offer['enabled'] else 'v_' + uuid4().hex)
    blob = await app.contents.put_bytes(canonical(listing_body(offer)), 'application/json')
    revision = Revision(format_version=1, id=revision_id, resource_id=offer_id,
        parents=(resource.revision,) if resource.revision else (), content=blob, relations=(),
        actor=ROOT_SUBJECT, subject=ROOT_SUBJECT, author=ROOT_SUBJECT, created_at=now,
        manifest_digest='', source_kind='operation', source_version=1,
        source_digest=source_digest or digest({'request_id': request_id, 'offer': offer}))
    manifest = {k: v for k, v in wire(revision).items() if k not in {'manifest_digest', 'signature'}}
    revision = replace(revision, manifest_digest=digest(manifest),
                       signature=signer.sign(canonical(manifest), purpose='revision'))
    tx.on_rollback(lambda: app.contents.unpin(blob, revision_id))
    await app.contents.pin(blob, revision_id)
    await app.contents.commit_revision('t_store', revision)
    await tx.append_revision(revision)
    await tx.replace(replace(resource, generation=resource.generation + 1,
                             revision=revision_id, modified_at=now, modified_by=ROOT_SUBJECT),
                     resource.generation)
    if new:
        tx.execute('INSERT INTO server_offer_resources(offer_id,listing_id) VALUES (?,?)',
                   (offer_id, offer_id), write=True)
    return revision_id
