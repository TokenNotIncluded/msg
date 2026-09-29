"""Explicit local adoption of a legacy offer; no financial rows are rewritten."""

import re
from uuid import uuid4

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import require
from msg.core.models import AuditEvent, Event, ResourceRef
from msg.market.compatibility import purchase_order, read_purchase
from msg.market.offer_resources import OFFER_COLUMNS, mapped_listing, write_offer_revision
from msg.plugins.offers import validate_local_offer


async def preview_import(app, tx, offer_id, *, revision_id=None):
    require(
        isinstance(offer_id, str) and re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,79}', offer_id),
        'invalid_offer_id',
    )
    require(mapped_listing(tx, offer_id) is None, 'offer_already_resource')
    row = tx.one(
        'SELECT ' + ','.join(OFFER_COLUMNS) + ' FROM server_offers WHERE offer_id=?', (offer_id,)
    )
    require(row is not None, 'offer_not_found')
    offer = dict(zip(OFFER_COLUMNS, row, strict=True))
    validate_local_offer(
        app,
        **{
            k: offer[k]
            for k in (
                'resource_kind',
                'unit',
                'price_minor',
                'min_quantity',
                'max_quantity',
                'entitlement_kind',
                'duration_seconds',
            )
        },
    )
    require(
        tx.one(
            'SELECT 1 FROM resources WHERE id=? OR (parent=? AND name=?)',
            (offer_id, 't_store', offer_id),
        )
        is None,
        'offer_resource_conflict',
    )
    revision_id = revision_id or 'v_' + uuid4().hex
    require(
        isinstance(revision_id, str) and re.fullmatch(r'v_[a-f0-9]{32}', revision_id),
        'invalid_revision_id',
    )
    require(
        tx.one('SELECT 1 FROM revisions WHERE id=?', (revision_id,)) is None,
        'offer_revision_conflict',
    )
    # The original serialized snapshots and receipt bytes are hashed unchanged.
    purchases, transactions, grants = [], {}, set()
    for pid, owner, encoded in tx.rows(
        'SELECT id,subject_id,offer_snapshot FROM money_purchases ORDER BY id'
    ):
        snapshot = loads(encoded)
        require(isinstance(snapshot, dict), 'legacy_purchase_invalid')
        if snapshot.get('offer_id') != offer_id:
            continue
        purchase = read_purchase(tx, pid, owner)
        order = purchase_order(tx, purchase)
        purchases.append(tx.one('SELECT * FROM money_purchases WHERE id=?', (pid,)))
        for index, tid in enumerate(order['receipt_refs']):
            kind, reference = tx.one('SELECT kind,reference FROM money_ledger WHERE id=?', (tid,))
            expected_kind = (
                'transfer'
                if index == 0
                else 'redeem'
                if purchase['state'] == 'settled'
                else 'refund'
            )
            expected_reference = (
                'purchase_fund:'
                if index == 0
                else 'purchase_settle:'
                if purchase['state'] == 'settled'
                else 'purchase_refund:'
            ) + pid
            require(
                (kind, reference) == (expected_kind, expected_reference),
                'offer_import_inconsistent_ledger',
            )
            transactions[tid] = tx.one('SELECT * FROM money_ledger WHERE id=?', (tid,))
        if purchase['entitlement_id'] is not None:
            grant = tx.one(
                'SELECT offer_id,quantity,entitlement_kind FROM resource_entitlements WHERE id=?',
                (purchase['entitlement_id'],),
            )
            require(
                grant == (offer_id, purchase['quantity'], snapshot['entitlement_kind']),
                'offer_import_inconsistent_entitlements',
            )
            grants.add(purchase['entitlement_id'])
    entitlements = tx.rows(
        'SELECT * FROM resource_entitlements WHERE offer_id=? ORDER BY id', (offer_id,)
    )
    require({row[0] for row in entitlements} == grants, 'offer_import_inconsistent_entitlements')
    return {
        'action': 'offer_import',
        'version': 1,
        'offer_id': offer_id,
        'before': offer,
        'listing_id': offer_id,
        'revision_id': revision_id,
        'purchase_count': len(purchases),
        'entitlement_count': len(entitlements),
        'dependencies_digest': digest({
            'purchases': purchases,
            'transactions': transactions,
            'entitlements': entitlements,
        }),
    }


async def apply_import(app, signer, plan, *, operator):
    require(signer.public_key == app.certificates.root_public_key, 'root_key_mismatch')
    now, request_id = app.clock(), 'local_offer_import_' + uuid4().hex
    async with app.metadata.transaction(write=True) as tx:
        current = await preview_import(app, tx, plan['offer_id'], revision_id=plan['revision_id'])
        require(current == plan, 'offer_import_preview_stale')
        approved_digest = digest(plan)
        await write_offer_revision(
            app,
            tx,
            signer,
            current['before'],
            now=now,
            request_id=request_id,
            new=True,
            revision_id=plan['revision_id'],
            source_digest=approved_digest,
        )
        statement = {
            'action': 'offer_import',
            'plan': current,
            'approved_digest': approved_digest,
            'operator': operator,
            'request_id': request_id,
            'time': wire(now),
        }
        signature = wire(signer.sign(canonical(statement), purpose='money-admin'))
        event = Event(
            id='audit_' + uuid4().hex,
            type='root.money.offer.import',
            time=now,
            request_id=request_id,
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            resources=(ResourceRef(id=plan['listing_id'], revision=plan['revision_id']),),
            data={**statement, 'root_signature': signature},
        )
        await tx.append_event(event)
        await tx.append_audit(
            AuditEvent(
                event=event,
                authority=(ResourceRef(id=app.certificates.root_certificate.resource_id),),
                before_digest=digest(plan['before']),
                after_digest=digest(plan['before']),
                previous_digest=None,
                entry_digest='',
                result='offer_import',
            )
        )
    return {
        'action': 'offer_import',
        'offer_id': plan['offer_id'],
        'listing_id': plan['listing_id'],
        'revision_id': plan['revision_id'],
        'approved_digest': approved_digest,
        'audit_event_id': event.id,
    }
