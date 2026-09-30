"""Root-approved account archival; identities and financial history are never erased."""

from dataclasses import replace

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, wire
from msg.core.errors import require
from msg.core.models import AuditEvent, Event, ResourceRef
from msg.plugins.common import new_id
from msg.plugins.money import _balance


async def archive_preview(tx, subject_id):
    user = await tx.resource(subject_id)
    subject = await tx.subject(subject_id)
    require(
        user.type == 'user'
        and subject.kind == 'registered'
        and not subject.local_only
        and user.owner == subject_id,
        'account_not_archivable',
    )
    require(not tx.setting('identity_archived:' + subject_id), 'account_already_archived')
    require(user.state == 'active', 'account_not_active')
    role = tx.one('SELECT status FROM money_bank_roles WHERE subject_id=?', (subject_id,))
    require(role is None or role[0] != 'active', 'bank_role_removal_required')
    balance = _balance(tx, subject_id)
    require(balance == 0, 'account_balance_not_zero')
    return {
        'action': 'archive_account',
        'subject_id': subject_id,
        'handle': user.name,
        'generation': user.generation,
        'auth_version': subject.auth_version,
        'balance_minor': balance,
        'credentials': tx.one('SELECT COUNT(*) FROM credentials WHERE subject=?', (subject_id,))[0],
        'owned_resources': tx.one('SELECT COUNT(*) FROM resources WHERE owner=?', (subject_id,))[0],
        'history_preserved': True,
    }


async def archive_account(app, subject_id, signer, *, expected_digest, operator):
    require(signer.public_key == app.certificates.root_public_key, 'root_key_mismatch')
    async with app.metadata.transaction(write=True) as tx:
        preview = await archive_preview(tx, subject_id)
        require(digest(preview) == expected_digest, 'account_archive_preview_changed')
        subject = await tx.subject(subject_id)
        user = await tx.resource(subject_id)
        now = app.clock()
        for row in tx.rows('SELECT id FROM credentials WHERE subject=?', (subject_id,)):
            credential = await tx.credential(row[0])
            if credential.revoked_at is None:
                await tx.save_credential(replace(credential, revoked_at=now), subject.auth_version)
        await tx.update_identity(
            replace(subject, auth_version=subject.auth_version + 1), subject.auth_version
        )
        await tx.replace(
            replace(
                user,
                state='archived',
                mode=0o700,
                generation=user.generation + 1,
                modified_at=now,
                modified_by=ROOT_SUBJECT,
            ),
            user.generation,
        )
        statement = {**preview, 'operator': operator, 'time': wire(now)}
        signature = wire(signer.sign(canonical(statement), purpose='account-admin'))
        tx.set_setting('identity_archived:' + subject_id, {**statement, 'signature': signature})
        tx.set_setting('authorization_epoch', tx.setting('authorization_epoch', 0) + 1)
        event = Event(
            id=new_id('audit'),
            type='root.identity.archive',
            time=now,
            request_id=new_id('local'),
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            resources=(ResourceRef(id=subject_id),),
            data={**statement, 'signature': signature},
        )
        await tx.append_audit(
            AuditEvent(
                event=event,
                authority=(ResourceRef(id=app.certificates.root_certificate.resource_id),),
                before_digest=digest(preview),
                after_digest=digest(statement),
                previous_digest=None,
                entry_digest='',
                result='archived',
            )
        )
    return {
        'subject_id': subject_id,
        'archived': True,
        'credentials_revoked': preview['credentials'],
        'history_preserved': True,
        'audit_event_id': event.id,
    }
