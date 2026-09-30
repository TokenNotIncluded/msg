"""Owner-signed transitions for an existing custodial upgrade operation."""

from __future__ import annotations

import hmac
from dataclasses import replace

from msg.core.codec import b64, canonical, decode, digest, loads, parse_time, unb64, wire
from msg.core.errors import require
from msg.core.models import AuditEvent, Credential, Event, HandlerOutput, ResourceRef, Signature
from msg.plugins.common import create_resource, new_id
from msg.security.age_keys import public_from_recipient
from msg.security.crypto import verify
from msg.security.custodial_migration import (
    ordered as ordered,
    output_refs as output_refs,
    owned_age_inventory as owned_age_inventory,
    snapshot as snapshot,
    stage_statement as stage_statement,
)
from msg.security.vault import open_signer, seal_retired_encryption_key, server_upgrade_proof


def activate_encryption_target(tx, subject, challenge, details, now):
    """After both possession proofs, new writes must no longer target the vault key."""
    key = details['new_encryption_key_id']
    current = tx.one(
        'SELECT key_id FROM encryption_subkeys WHERE subject=? AND is_primary=1', (subject,)
    )
    if current == (key,):
        return
    require(current == (details['old_encryption_key_id'],), 'custodial_encryption_target_changed')
    tx.execute(
        'UPDATE encryption_subkeys SET is_primary=0,retired_at=? WHERE subject=? AND is_primary=1',
        (wire(now), subject),
        write=True,
    )
    tx.execute(
        'INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
        (
            key,
            subject,
            challenge['encryption_recipient'],
            b64(public_from_recipient(challenge['encryption_recipient'])),
            wire(now),
            None,
            1,
        ),
        write=True,
    )


async def audit(tx, ctx, request, subject, kind, data, *, before=None):
    event = Event(
        id=new_id('audit'),
        type=kind,
        time=ctx.now,
        request_id=request.request_id,
        actor=ctx.principal.actor,
        subject=subject,
        resources=(ResourceRef(id=subject),),
        data=data,
    )
    await tx.append_audit(
        AuditEvent(
            event=event,
            authority=(ResourceRef(id=subject),),
            before_digest=digest(before) if before is not None else None,
            after_digest=digest(data),
            previous_digest=None,
            entry_digest='',
            result='committed',
        )
    )


def authorize_migration(ctx, subject, credential_id, status, details):
    if subject.kind == 'custodial':
        require(
            ctx.principal.method == 'token' and ctx.principal.credential_id == credential_id,
            'custodial_token_required',
        )
        require(status in {'pending', 'pending_rewrap'}, 'custodial_upgrade_not_pending')
    else:
        require(
            subject.kind == 'registered'
            and ctx.principal.method == 'signature'
            and ctx.principal.credential_id == details['new_identity_key_id']
            and status == 'completed',
            'custodial_upgrade_new_key_required',
        )


async def transition(app, ctx, request, tx, subject):
    args = request.arguments
    row = tx.one(
        """SELECT credential_id,status,expires_at,challenge,ephemeral_nonce,
        ephemeral_ciphertext,body FROM custodial_upgrades WHERE id=? AND subject=?""",
        (args['challenge_id'], subject.resource_id),
    )
    require(row is not None, 'custodial_upgrade_not_pending')
    challenge, details = loads(row[3]), loads(row[6])
    authorize_migration(ctx, subject, row[0], row[1], details)
    verify(
        unb64(challenge['public_key']),
        canonical(stage_statement(subject.resource_id, args, request.request_id)),
        decode(Signature, args['migration_ack']),
        purpose='custodial-decision-v1',
    )
    if row[1] == 'pending':
        require(
            args['action'] == 'refresh' and ctx.now < parse_time(row[2]),
            'custodial_upgrade_not_pending_rewrap',
        )
        expected = server_upgrade_proof(app, subject.resource_id, challenge, row[4], row[5])
        require(hmac.compare_digest(expected, args['age_proof']), 'age_possession_failed')
    else:
        require(
            hmac.compare_digest(details['age_possession_digest'], digest(args['age_proof'])),
            'age_possession_failed',
        )
    state = await snapshot(tx, subject.resource_id, row[1], challenge, details, now=ctx.now)
    state.require_observation(args)
    before = digest(details)
    if args['action'] == 'refresh':
        # Preserve disappeared history as unresolved work; never delete it from a
        # manifest just because the source is no longer enumerable.
        added = state.data['delta']['added']
        require(added or state.data['delta']['missing'], 'custodial_inventory_unchanged')
        details['age_inventory'] = ordered(details['age_inventory'] + added)
        details['inventory_version'] = details.get('inventory_version', 1) + 1
        source_keys = details.setdefault('source_keys', {})
        for item in added:
            key = tx.setting('keystore_key:' + item['revision'], details['old_encryption_key_id'])
            source_keys[item['revision']] = key
            if key == details['new_encryption_key_id']:
                details.setdefault('rewrap_mappings', {})[item['revision']] = {
                    'old': item,
                    'new': item,
                    'old_key_id': key,
                    'new_key_id': key,
                    'recipient': challenge['encryption_recipient'],
                    'copy_kind': 'new_key_write',
                    'created_at': wire(ctx.now),
                }
        details['last_delta'] = dict(state.data['delta'], version=details['inventory_version'])
        details.pop('resolution', None)
    elif args['action'] == 'recovery_envelope':
        state.unchanged()
        require(row[1] != 'pending', 'custodial_upgrade_not_pending_rewrap')
        if details.get('recovery_envelope') is None:
            ciphertext = seal_retired_encryption_key(
                app,
                tx,
                subject.resource_id,
                details['old_encryption_key_id'],
                challenge['encryption_recipient'],
                args['challenge_id'],
            )
            folder = tx.one(
                "SELECT id FROM resources WHERE parent=? AND name='keystore'",
                (subject.resource_id,),
            )
            require(folder is not None, 'keystore_missing')
            resource = await create_resource(
                app,
                ctx,
                request,
                tx,
                parent=folder[0],
                type='keystore',
                name='retired-key-' + digest(args['challenge_id'])[7:31] + '.age',
                body=ciphertext,
                media_type='application/octet-stream',
                mode=0o600,
            )
            tx.set_setting('keystore_format:' + resource.revision, 'age')
            tx.set_setting('keystore_key:' + resource.revision, details['new_encryption_key_id'])
            details['recovery_envelope'] = {
                'subject_id': subject.resource_id,
                'challenge_id': args['challenge_id'],
                'old_key_id': details['old_encryption_key_id'],
                'new_key_id': details['new_encryption_key_id'],
                'recipient': challenge['encryption_recipient'],
                'purpose': 'retired-encryption-subkey-recovery',
                'method': 'compatibility_recovery',
                'ciphertext': {
                    'id': resource.id,
                    'revision': resource.revision,
                    'ciphertext_digest': digest(ciphertext),
                },
            }
    else:
        require(args['action'] == 'finalize', 'invalid_custodial_transition')
        resolution = args.get('resolution')
        if resolution == 'verified' or state.data['delta']['added']:
            state.unchanged()
        require(
            not (
                row[1] == 'completed' and state.data['retirement']['online_encryption_key_deleted']
            ),
            'custodial_upgrade_already_finalized',
        )
        unresolved = state.data['unresolved_revisions']
        require(
            args.get('reviewed_policy_version') == state.data['recovery_policy_version'],
            'custodial_recovery_policy_review_required',
        )
        if resolution == 'verified':
            require(state.data['history_recoverable'], 'custodial_history_ack_required')
            require(not args.get('loss_revisions'), 'custodial_loss_scope_mismatch')
        else:
            require(
                resolution in {'accept_loss', 'retain_decrypt'} and args.get('reason'),
                'custodial_resolution_required',
            )
            require(
                sorted(args.get('loss_revisions', ())) == unresolved,
                'custodial_loss_scope_mismatch',
            )
            if resolution == 'accept_loss':
                require(bool(unresolved), 'custodial_loss_scope_mismatch')
        require(
            resolution == 'retain_decrypt' or args['external_ciphertexts_migrated'],
            'custodial_external_history_unresolved',
        )
        details['resolution'] = {
            'decision': resolution,
            'revisions': unresolved,
            'reason': args.get('reason'),
            'inventory_digest': state.inventory_digest,
            'results_digest': state.data['results_digest'],
            'request_id': request.request_id,
            'signature': wire(args['migration_ack']),
            'reviewed_policy_version': args['reviewed_policy_version'],
            'external_migration': 'owner_declared_unverified'
            if args['external_ciphertexts_migrated']
            else 'unknown_retained',
        }
        details['external_ciphertexts_migrated'] = args['external_ciphertexts_migrated']
        return await switch_identity(
            app,
            ctx,
            request,
            tx,
            subject,
            challenge,
            details,
            retain_decrypt=resolution == 'retain_decrypt',
        )
    tx.execute(
        'UPDATE custodial_upgrades SET body=? WHERE id=?',
        (canonical(details).decode(), args['challenge_id']),
        write=True,
    )
    await audit(
        tx,
        ctx,
        request,
        subject.resource_id,
        'identity.custodial_' + args['action'],
        {
            'challenge_id': args['challenge_id'],
            'before_digest': before,
            'after_digest': digest(details),
            'decision_signature': wire(args['migration_ack']),
        },
    )
    return HandlerOutput(
        data=(await snapshot(tx, subject.resource_id, row[1], challenge, details, now=ctx.now)).data
    )


async def switch_identity(
    app, ctx, request, tx, subject, challenge, details, *, retain_decrypt=False
):
    """One SQL transaction: switch identity, revoke tokens, and restrict/delete online keys."""
    from msg.plugins.identity import issue_online

    before = digest(details)
    uid = subject.resource_id
    new_key = details['new_identity_key_id']
    switched = subject.kind == 'registered'
    if not switched:
        require(subject.kind == 'custodial', 'custodial_token_required')
        phase = tx.one(
            'SELECT status FROM custodial_upgrades WHERE id=? AND subject=?',
            (challenge['challenge_id'], uid),
        )
        require(phase is not None, 'custodial_upgrade_not_pending')
        signer = open_signer(app, tx, uid)
        endorsement = signer.sign(
            canonical({
                'subject_id': uid,
                'challenge_id': challenge['challenge_id'],
                'old_identity_key_id': details['old_identity_key_id'],
                'new_identity_key_id': new_key,
                'new_encryption_key_id': details['new_encryption_key_id'],
                'inventory_digest': (
                    await snapshot(tx, uid, phase[0], challenge, details)
                ).inventory_digest,
            }),
            purpose='custodial-upgrade',
        )
        revoked = []
        for (raw,) in tx.rows('SELECT body FROM credentials WHERE subject=?', (uid,)):
            credential = decode(Credential, loads(raw))
            if credential.revoked_at is None and credential.kind in {'token', 'signing_key'}:
                await tx.save_credential(
                    replace(credential, revoked_at=ctx.now), subject.auth_version
                )
                revoked.append(credential.id)
        credential = Credential(
            id=new_key,
            subject_id=uid,
            kind='signing_key',
            verifier=unb64(challenge['public_key']),
            ceiling=app.primary_ceiling(),
            not_before=ctx.now,
            expires_at=None,
            revoked_at=None,
        )
        await tx.save_credential(credential, subject.auth_version)
        tx.execute(
            'UPDATE identity_keys SET is_primary=0,retired_at=? WHERE subject=? AND is_primary=1',
            (wire(ctx.now), uid),
            write=True,
        )
        tx.execute(
            'INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
            (new_key, uid, challenge['public_key'], wire(ctx.now), None, 1),
            write=True,
        )
        activate_encryption_target(tx, uid, challenge, details, ctx.now)
        resource = await tx.resource(uid)
        from msg.core.handles import check_handle_change

        name, _ = check_handle_change(tx, resource, challenge['handle'], ctx.now, record=True)
        await tx.replace(
            replace(
                resource,
                name=name,
                generation=resource.generation + 1,
                modified_at=ctx.now,
                modified_by=ctx.principal.actor,
            ),
            resource.generation,
        )
        await tx.update_identity(
            replace(subject, kind='registered', auth_version=subject.auth_version + 1),
            subject.auth_version,
        )
        certificate = await issue_online(
            app,
            tx,
            uid,
            new_key,
            ctx,
            request,
            authority_source={
                'kind': 'custodial_upgrade',
                'challenge_id': challenge['challenge_id'],
                'new_key_id': new_key,
            },
        )
        completed = {
            'status': 'completed',
            'subject_id': uid,
            'identity_key_id': new_key,
            'encryption_key_id': details['new_encryption_key_id'],
            'encryption_recipient': challenge['encryption_recipient'],
            'certificate_id': certificate.resource_id,
            'previous_credential': ctx.principal.credential_id,
            'credential_id': new_key,
            'signature_source': 'custodial',
        }
        await audit(
            tx,
            ctx,
            request,
            uid,
            'identity.custodial_tokens_revoked',
            {
                'challenge_id': challenge['challenge_id'],
                'credentials': revoked,
                'new_key_id': new_key,
                'old_key_endorsement': wire(endorsement),
            },
        )
    else:
        completed = dict(details['completed_result'])
    if retain_decrypt:
        tx.execute(
            """UPDATE custodial_vault SET status='decrypt_only',signing_nonce=NULL,
            signing_ciphertext=NULL WHERE subject=?""",
            (uid,),
            write=True,
        )
    else:
        tx.execute(
            """UPDATE custodial_vault SET status='destroyed',signing_nonce=NULL,
            signing_ciphertext=NULL,age_nonce=NULL,age_ciphertext=NULL,destroyed_at=? WHERE subject=?""",
            (wire(ctx.now), uid),
            write=True,
        )
    current = await snapshot(tx, uid, 'completed', challenge, details, now=ctx.now)
    completed.update(
        completion_scope='identity_switch_only',
        phase=current.data['phase'],
        history_recoverable=current.data['history_recoverable'],
        verification_scope=current.data['verification_scope'],
        server_tracked_age_revisions=len(details['age_inventory']),
        vault_destroyed=not retain_decrypt,
        external_migration='owner_declared_unverified',
        external_ciphertexts_verified=False,
        retirement=current.data['retirement'],
        resolution=details.get('resolution'),
        challenge_id=challenge['challenge_id'],
        finalization_request_id=request.request_id,
    )
    details['completed_result'] = completed
    tx.execute(
        """UPDATE custodial_upgrades SET status='completed',ephemeral_nonce=NULL,
        ephemeral_ciphertext=NULL,body=? WHERE id=?""",
        (canonical(details).decode(), challenge['challenge_id']),
        write=True,
    )
    await audit(
        tx,
        ctx,
        request,
        uid,
        'identity.custodial_online_key_retirement',
        {
            'challenge_id': challenge['challenge_id'],
            'old_identity_key_id': details['old_identity_key_id'],
            'old_encryption_key_id': details['old_encryption_key_id'],
            'retirement': current.data['retirement'],
            'resolution': details.get('resolution'),
            'before_digest': before,
        },
    )
    return HandlerOutput(resources=(ResourceRef(id=uid),), data=completed)
