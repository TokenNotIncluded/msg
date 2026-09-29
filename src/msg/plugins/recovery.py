"""Owner-declared recovery metadata. No account recovery or decryption authority."""

from __future__ import annotations

from msg.core.codec import b64, canonical, decode, digest, loads, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, ResourceRef, Signature
from msg.core.requests import signing_bytes
from msg.plugins.common import (
    assert_generation,
    check_access,
    create_resource,
    new_id,
    registration,
)
from msg.plugins.schemas import IDENTIFIER, REF, STRING, obj
from msg.security.age_keys import encryption_key_id, public_from_recipient
from msg.security.crypto import verify
from msg.security.custodial_migration import (
    ack_statement,
    keyed_source,
    keyed_target,
    snapshot as migration_snapshot,
)
from msg.security.vault import open_age_identity, rewrap_owned_age_ciphertext


def _current_policy(tx, subject):
    row = tx.one(
        'SELECT body FROM recovery_policies WHERE subject=? ORDER BY version DESC LIMIT 1',
        (subject,),
    )
    return dict(loads(row[0])) if row else None


def _owner(app, ctx, request, tx):
    from msg.plugins.identity import controlled_owner

    return controlled_owner(app, ctx, request, tx)


def install(app):
    op, finish = registration(app, 'recovery', ('identity',))

    async def migration(ctx, request, tx, *, pending=False):
        from msg.plugins.custodial_lifecycle import authorize_migration

        subject = await _owner(app, ctx, request, tx)
        row = tx.one(
            """SELECT credential_id,status,challenge,body FROM custodial_upgrades
            WHERE id=? AND subject=?""",
            (request.arguments['challenge_id'], subject.resource_id),
        )
        require(row is not None, 'custodial_upgrade_not_pending')
        challenge, details = loads(row[2]), loads(row[3])
        authorize_migration(ctx, subject, row[0], row[1], details)
        require(not pending or row[1] != 'pending', 'custodial_upgrade_not_pending_rewrap')
        state = await migration_snapshot(
            tx, subject.resource_id, row[1], challenge, details, now=ctx.now
        )
        return subject, row[1], challenge, details, state

    @op(
        'identity.custodial_upgrade_inventory',
        obj({'challenge_id': IDENTIFIER}, ('challenge_id',)),
        effect='read',
    )
    async def custodial_upgrade_inventory(ctx, request, tx):
        *_, state = await migration(ctx, request, tx)
        return HandlerOutput(data=state.data)

    rewrap_schema = obj(
        {
            'challenge_id': IDENTIFIER,
            'ciphertext_ref': REF,
            'old_encryption_key_id': IDENTIFIER,
            'new_recipient': STRING,
        },
        ('challenge_id', 'ciphertext_ref', 'old_encryption_key_id', 'new_recipient'),
    )

    async def rewrap(ctx, request, tx, *, current_only):
        from msg.plugins.custodial_lifecycle import activate_encryption_target, audit

        subject, status, challenge, details, state = await migration(ctx, request, tx, pending=True)
        state.unchanged()
        args = request.arguments
        require(
            challenge['encryption_recipient'] == args['new_recipient']
            and details['old_encryption_key_id'] == args['old_encryption_key_id'],
            'custodial_rewrap_key_mismatch',
        )
        vault = tx.one(
            'SELECT encryption_key_id,status FROM custodial_vault WHERE subject=?',
            (subject.resource_id,),
        )
        require(
            vault is not None
            and vault[0] == args['old_encryption_key_id']
            and vault[1] in {'active', 'decrypt_only'},
            'custodial_rewrap_unknown_old_key',
        )
        activate_encryption_target(tx, subject.resource_id, challenge, details, ctx.now)
        ref = decode(ResourceRef, args['ciphertext_ref'])
        require(ref.revision is not None, 'custodial_rewrap_revision_required')
        resource = await tx.resource(ref.id)
        folder = tx.one(
            "SELECT id FROM resources WHERE parent=? AND name='keystore'", (subject.resource_id,)
        )
        require(
            folder is not None
            and resource.parent == folder[0]
            and resource.type == 'keystore'
            and resource.owner == subject.resource_id
            and resource.state != 'purged',
            'custodial_rewrap_not_owned',
        )
        if current_only:
            await assert_generation(request, resource)
            require(resource.revision == ref.revision, 'revision_conflict')
        await check_access(app, ctx, request, tx, resource.id, 'read')
        revision = await tx.revision(ref)
        old = {
            'id': resource.id,
            'revision': revision.id,
            'ciphertext_digest': revision.content.digest,
        }
        require(old in details['age_inventory'], 'custodial_rewrap_not_in_frozen_inventory')
        require(
            keyed_source(old, details)['key_id'] == args['old_encryption_key_id'],
            'custodial_rewrap_key_mismatch',
        )
        require(
            revision.id not in details.get('rewrap_mappings', {}), 'custodial_rewrap_already_mapped'
        )
        require(
            tx.setting('keystore_format:' + revision.id) == 'age'
            and revision.content.size <= 1048576,
            'custodial_rewrap_age_required',
        )
        old_ciphertext = await app.contents.read_bytes(revision.content, limit=1048576)
        new_ciphertext = rewrap_owned_age_ciphertext(
            open_age_identity(app, tx, subject.resource_id), args['new_recipient'], old_ciphertext
        )
        copy = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent=folder[0],
            type='keystore',
            name='migration-' + digest((args['challenge_id'], revision.id))[7:31] + '.age',
            body=new_ciphertext,
            media_type='application/octet-stream',
            mode=0o600,
        )
        tx.set_setting('keystore_format:' + copy.revision, 'age')
        tx.set_setting('keystore_key:' + copy.revision, details['new_encryption_key_id'])
        new = {
            'id': copy.id,
            'revision': copy.revision,
            'ciphertext_digest': digest(new_ciphertext),
        }
        mapping = {
            'old': old,
            'new': new,
            'recipient': args['new_recipient'],
            'old_key_id': args['old_encryption_key_id'],
            'new_key_id': details['new_encryption_key_id'],
            'created_at': wire(ctx.now),
            'copy_kind': 'historical_revision',
        }
        details.setdefault('rewrap_mappings', {})[revision.id] = mapping
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
            request.operation,
            {
                'challenge_id': args['challenge_id'],
                'mapping': mapping,
                'old_revision_unchanged': True,
                'plaintext_exposed': False,
            },
        )
        return HandlerOutput(
            resources=(ResourceRef(id=copy.id, revision=copy.revision),),
            data={
                'mapping': mapping,
                'old_revision_unchanged': True,
                'client_decryption_acked': False,
                'client_decryption_verified': False,
                'vault_remains_active': vault[1] == 'active',
                'new_revision': copy.revision,
                'new_recipient': args['new_recipient'],
                'ciphertext_digest': digest(new_ciphertext),
            },
        )

    @op('identity.custodial_rewrap_entry', rewrap_schema)
    async def custodial_rewrap_entry(ctx, request, tx):
        return await rewrap(ctx, request, tx, current_only=True)

    @op('identity.custodial_rewrap_revision', rewrap_schema)
    async def custodial_rewrap_revision(ctx, request, tx):
        return await rewrap(ctx, request, tx, current_only=False)

    @op(
        'identity.custodial_migration_get',
        obj(
            {'challenge_id': IDENTIFIER, 'old_revision': IDENTIFIER},
            ('challenge_id', 'old_revision'),
        ),
        effect='read',
    )
    async def custodial_migration_get(ctx, request, tx):
        subject, status, challenge, details, state = await migration(ctx, request, tx, pending=True)
        revision_id = request.arguments['old_revision']
        source = next(
            (item for item in details['age_inventory'] if item['revision'] == revision_id), None
        )
        require(source is not None, 'custodial_migration_not_found')
        mapping = details.get('rewrap_mappings', {}).get(revision_id)
        envelope = details.get('recovery_envelope')
        require(mapping is not None or envelope is not None, 'custodial_migration_not_found')
        target = mapping['new'] if mapping else envelope['ciphertext']
        for item in (source, target):
            await check_access(app, ctx, request, tx, item['id'], 'read')
            revision = await tx.revision(ResourceRef(id=item['id'], revision=item['revision']))
            require(
                revision.content.digest == item['ciphertext_digest'],
                'custodial_migration_digest_mismatch',
            )
        ack = details.get('rewrap_acks' if mapping else 'recovery_acks', {}).get(revision_id)
        return HandlerOutput(
            output=ResourceRef(id=target['id'], revision=target['revision']),
            data={
                'status': status,
                'source': source,
                'mapped': mapping['new'] if mapping else None,
                'recovery_envelope': envelope if not mapping else None,
                'method': 'rewrap' if mapping else 'compatibility_recovery',
                'recipient': challenge['encryption_recipient'],
                'client_ack': ack,
                'old_revision_unchanged': True,
            },
        )

    @op(
        'identity.custodial_rewrap_ack',
        obj(
            {
                'challenge_id': IDENTIFIER,
                'old_revision': IDENTIFIER,
                'new_revision': IDENTIFIER,
                'ciphertext_digest': IDENTIFIER,
                'plaintext_digest': IDENTIFIER,
                'decryption_ack': {'type': 'object'},
            },
            (
                'challenge_id',
                'old_revision',
                'new_revision',
                'ciphertext_digest',
                'plaintext_digest',
                'decryption_ack',
            ),
        ),
    )
    @op(
        'identity.custodial_rewrap_ack',
        obj(
            {
                'challenge_id': IDENTIFIER,
                'old_revision': IDENTIFIER,
                'new_revision': IDENTIFIER,
                'ciphertext_digest': IDENTIFIER,
                'plaintext_digest': IDENTIFIER,
                'decryption_ack': {'type': 'object'},
                'inventory_digest': IDENTIFIER,
                'method': {'enum': ['rewrap', 'compatibility_recovery']},
            },
            (
                'challenge_id',
                'old_revision',
                'new_revision',
                'ciphertext_digest',
                'plaintext_digest',
                'decryption_ack',
            ),
        ),
        version=2,
    )
    async def custodial_rewrap_ack(ctx, request, tx):
        from msg.plugins.custodial_lifecycle import audit

        subject, status, challenge, details, state = await migration(ctx, request, tx, pending=True)
        state.unchanged()
        args = request.arguments
        source = next(
            (item for item in details['age_inventory'] if item['revision'] == args['old_revision']),
            None,
        )
        require(source is not None, 'custodial_rewrap_not_in_frozen_inventory')
        method = args.get('method', 'rewrap')
        mapping = details.get('rewrap_mappings', {}).get(args['old_revision'])
        envelope = details.get('recovery_envelope')
        if method == 'rewrap':
            require(mapping is not None, 'custodial_rewrap_mapping_mismatch')
            target = mapping['new']
            committed_target = keyed_target(mapping, details)
        else:
            require(
                envelope is not None
                and keyed_source(source, details)['key_id'] == envelope['old_key_id'],
                'custodial_recovery_key_mismatch',
            )
            target = envelope['ciphertext']
            committed_target = envelope
        require(
            target['revision'] == args['new_revision']
            and target['ciphertext_digest'] == args['ciphertext_digest'],
            'custodial_rewrap_mapping_mismatch',
        )
        revision = await tx.revision(ResourceRef(id=target['id'], revision=target['revision']))
        require(
            revision.content.digest == target['ciphertext_digest'],
            'custodial_rewrap_mapping_mismatch',
        )
        require(
            args['plaintext_digest'].startswith('sha256:')
            and len(args['plaintext_digest']) == 71
            and all(c in '0123456789abcdef' for c in args['plaintext_digest'][7:]),
            'invalid_plaintext_digest',
        )
        scoped = 'inventory_digest' in args
        if scoped:
            require(
                args['inventory_digest'] == state.inventory_digest,
                'custodial_migration_preview_stale',
            )
            signed = ack_statement(
                subject.resource_id,
                args['challenge_id'],
                state.inventory_digest,
                keyed_source(source, details),
                committed_target,
                args['plaintext_digest'],
                request.request_id,
                method=method,
            )
            purpose = 'custodial-history-ack-v1'
        else:
            require(method == 'rewrap', 'custodial_inventory_ack_required')
            signed = {
                'subject_id': subject.resource_id,
                'challenge_id': args['challenge_id'],
                'old': source,
                'new': target,
                'plaintext_digest': args['plaintext_digest'],
                'request_id': request.request_id,
            }
            purpose = 'custodial-rewrap-ack'
        verify(
            unb64(challenge['public_key']),
            canonical(signed),
            decode(Signature, args['decryption_ack']),
            purpose=purpose,
        )
        bucket = details.setdefault('rewrap_acks' if method == 'rewrap' else 'recovery_acks', {})
        previous = bucket.get(args['old_revision'])
        require(
            previous is None or previous.get('inventory_digest') != args.get('inventory_digest'),
            'custodial_rewrap_already_acked',
        )
        ack = {
            'old_revision': args['old_revision'],
            'new_revision': args['new_revision'],
            'ciphertext_digest': args['ciphertext_digest'],
            'plaintext_digest': args['plaintext_digest'],
            'signature': wire(args['decryption_ack']),
            'request_id': request.request_id,
            'verified_at': wire(ctx.now),
            'method': method,
        }
        if scoped:
            ack['inventory_digest'] = state.inventory_digest
        bucket[args['old_revision']] = ack
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
            'identity.custodial_rewrap_ack',
            {
                'challenge_id': args['challenge_id'],
                'ack': ack,
                'verification_scope': 'enumerated_revision_only',
            },
        )
        final = await migration_snapshot(tx, subject.resource_id, status, challenge, details)
        return HandlerOutput(
            data={
                'challenge_id': args['challenge_id'],
                'old_revision': args['old_revision'],
                'new_revision': args['new_revision'],
                'client_decryption_acked': True,
                'inventory_bound': scoped,
                'vault_remains_active': final.data['vault_remains_active'],
                'finalize_ready': final.data['finalize_ready'],
            }
        )

    @op('identity.recovery_custodians', obj(), effect='read')
    async def custodians(ctx, request, tx):
        return HandlerOutput(
            data={'items': [wire(item) for item in app.settings.recovery_custodians]}
        )

    @op('identity.recovery_policy_get', obj(), effect='read')
    async def policy_get(ctx, request, tx):
        subject = await _owner(app, ctx, request, tx)
        policy = _current_policy(tx, subject.resource_id)
        return HandlerOutput(
            data=policy
            or {
                'subject_id': subject.resource_id,
                'version': 0,
                'encryption_key_id': None,
                'recipients': [],
                'opted_in': False,
                'recipient_claim': 'owner_declared_unverified',
            }
        )

    recipient_schema = obj({'recipient': STRING, 'custodian_ref': IDENTIFIER}, ('recipient',))

    @op(
        'identity.recovery_policy_set',
        obj(
            {
                'expected_version': {'type': 'integer', 'minimum': 0},
                'encryption_key_id': IDENTIFIER,
                'recipients': {'type': 'array', 'items': recipient_schema, 'maxItems': 8},
            },
            ('expected_version', 'encryption_key_id', 'recipients'),
        ),
        signature=True,
    )
    async def policy_set(ctx, request, tx):
        subject = await _owner(app, ctx, request, tx)
        current = _current_policy(tx, subject.resource_id)
        version = current['version'] if current else 0
        args = request.arguments
        require(args['expected_version'] == version, 'recovery_policy_version_conflict')
        key = tx.one(
            'SELECT key_id FROM encryption_subkeys WHERE subject=? AND is_primary=1',
            (subject.resource_id,),
        )
        require(
            key is not None and args['encryption_key_id'] == key[0],
            'recovery_encryption_key_mismatch',
        )
        known = {item.id: item for item in app.settings.recovery_custodians}
        recipients = []
        for entry in args['recipients']:
            recipient = entry['recipient']
            fingerprint = encryption_key_id(public_from_recipient(recipient))
            custodian_ref = entry.get('custodian_ref')
            if custodian_ref is not None:
                if custodian_ref in known:
                    require(
                        known[custodian_ref].recipient == recipient, 'custodian_recipient_mismatch'
                    )
                else:
                    require(custodian_ref.startswith('u_'), 'unknown_custodian_ref')
                    await tx.subject(custodian_ref)
            recipients.append({
                'recipient': recipient,
                'fingerprint': fingerprint,
                'custodian_ref': custodian_ref,
            })
        require(
            len({item['fingerprint'] for item in recipients}) == len(recipients),
            'duplicate_recovery_recipient',
        )
        policy = {
            'subject_id': subject.resource_id,
            'version': version + 1,
            'encryption_key_id': args['encryption_key_id'],
            'recipients': recipients,
            'opted_in': bool(recipients),
            'recipient_claim': 'owner_declared_unverified',
            'created_at': wire(ctx.now),
            'signature_source': 'self-custody',
            'request_signature': wire(request.proof.signature),
            'signed_envelope': b64(signing_bytes(request)),
        }
        tx.execute(
            'INSERT INTO recovery_policies VALUES (?,?,?,?,?)',
            (
                subject.resource_id,
                version + 1,
                args['encryption_key_id'],
                wire(ctx.now),
                canonical(policy).decode(),
            ),
            write=True,
        )
        return HandlerOutput(data=policy)

    envelope_schema = obj(
        {
            'ciphertext_ref': REF,
            'encryption_key_id': IDENTIFIER,
            'policy_version': {'type': 'integer', 'minimum': 1},
            'recipient_fingerprints': {
                'type': 'array',
                'items': IDENTIFIER,
                'minItems': 1,
                'maxItems': 8,
                'uniqueItems': True,
            },
            'custodian_refs': {
                'type': 'array',
                'items': IDENTIFIER,
                'maxItems': 8,
                'uniqueItems': True,
            },
            'purpose': {'const': 'encryption-subkey-recovery'},
            'instructions_ref': REF,
        },
        (
            'ciphertext_ref',
            'encryption_key_id',
            'policy_version',
            'recipient_fingerprints',
            'purpose',
        ),
    )

    @op('identity.recovery_envelope_register', envelope_schema, signature=True)
    async def envelope_register(ctx, request, tx):
        subject = await _owner(app, ctx, request, tx)
        args = request.arguments
        policy = _current_policy(tx, subject.resource_id)
        require(
            policy is not None
            and policy['opted_in']
            and args['policy_version'] == policy['version'],
            'recovery_policy_not_opted_in',
        )
        primary = tx.one(
            'SELECT key_id FROM encryption_subkeys WHERE subject=? AND is_primary=1',
            (subject.resource_id,),
        )
        require(
            primary is not None
            and args['encryption_key_id'] == primary[0]
            and args['encryption_key_id'] == policy['encryption_key_id'],
            'recovery_encryption_key_mismatch',
        )
        offered = set(args['recipient_fingerprints'])
        allowed = {entry['fingerprint']: entry for entry in policy['recipients']}
        require(offered <= allowed.keys(), 'recovery_recipient_not_in_policy')
        expected_refs = {
            entry['custodian_ref']
            for fingerprint, entry in allowed.items()
            if fingerprint in offered and entry['custodian_ref'] is not None
        }
        refs = set(args.get('custodian_refs', ()))
        require(refs == expected_refs, 'recovery_custodian_ref_mismatch')
        ref = decode(ResourceRef, args['ciphertext_ref'])
        require(ref.revision is not None, 'recovery_revision_required')
        resource = await tx.resource(ref.id)
        folder = tx.one(
            "SELECT id FROM resources WHERE parent=? AND name='keystore'", (subject.resource_id,)
        )
        require(
            folder is not None
            and resource.type == 'keystore'
            and resource.owner == subject.resource_id
            and resource.parent == folder[0],
            'recovery_ciphertext_not_owned',
        )
        await check_access(app, ctx, request, tx, ref.id, 'read')
        await tx.revision(ref)
        require(resource.revision == ref.revision, 'recovery_revision_not_current')
        require(
            tx.setting('keystore_format:' + ref.revision) == 'age',
            'recovery_age_ciphertext_required',
        )
        instructions = None
        if args.get('instructions_ref'):
            instructions = decode(ResourceRef, args['instructions_ref'])
            instruction_resource = await tx.resource(instructions.id)
            require(
                instruction_resource.owner == subject.resource_id, 'recovery_instructions_not_owned'
            )
            await check_access(app, ctx, request, tx, instructions.id, 'read')
            if instructions.revision is not None:
                await tx.revision(instructions)
        record = {
            'id': new_id('renv'),
            'owner_subject': subject.resource_id,
            'ciphertext_ref': wire(ref),
            'encryption_key_id': args['encryption_key_id'],
            'policy_version': policy['version'],
            'recipient_fingerprints': sorted(offered),
            'custodian_refs': sorted(refs),
            'purpose': args['purpose'],
            'instructions_ref': wire(instructions) if instructions else None,
            'created_at': wire(ctx.now),
            'recipient_claim': 'owner_declared_unverified',
            'signature_source': 'self-custody',
            'request_signature': wire(request.proof.signature),
            'signed_envelope': b64(signing_bytes(request)),
        }
        tx.execute(
            """INSERT INTO recovery_envelopes
            (id,owner,ciphertext_resource,ciphertext_revision,policy_version,encryption_key_id,
             purpose,created_at,body) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                record['id'],
                subject.resource_id,
                ref.id,
                ref.revision,
                policy['version'],
                args['encryption_key_id'],
                args['purpose'],
                record['created_at'],
                canonical(record).decode(),
            ),
            write=True,
        )
        return HandlerOutput(resources=(ref,), data=record)

    @op('identity.recovery_envelope_get', obj({'id': IDENTIFIER}, ('id',)), effect='read')
    async def envelope_get(ctx, request, tx):
        subject = await _owner(app, ctx, request, tx)
        row = tx.one(
            'SELECT owner,body FROM recovery_envelopes WHERE id=?', (request.arguments['id'],)
        )
        require(row is not None and row[0] == subject.resource_id, 'recovery_envelope_not_found')
        record = loads(row[1])
        ref = decode(ResourceRef, record['ciphertext_ref'])
        await check_access(app, ctx, request, tx, ref.id, 'read')
        await tx.revision(ref)
        if record['instructions_ref'] is not None:
            instruction = decode(ResourceRef, record['instructions_ref'])
            await check_access(app, ctx, request, tx, instruction.id, 'read')
            if instruction.revision is not None:
                await tx.revision(instruction)
        return HandlerOutput(data=record)

    @op('identity.recovery_envelope_list', obj(), effect='read')
    async def envelope_list(ctx, request, tx):
        subject = await _owner(app, ctx, request, tx)
        items = []
        for (raw,) in tx.rows(
            'SELECT body FROM recovery_envelopes WHERE owner=? ORDER BY created_at,id',
            (subject.resource_id,),
        ):
            record = loads(raw)
            ref = decode(ResourceRef, record['ciphertext_ref'])
            try:
                await check_access(app, ctx, request, tx, ref.id, 'read')
                await tx.revision(ref)
                if record['instructions_ref'] is not None:
                    instruction = decode(ResourceRef, record['instructions_ref'])
                    await check_access(app, ctx, request, tx, instruction.id, 'read')
                    if instruction.revision is not None:
                        await tx.revision(instruction)
            except Failure as exc:
                if exc.code in {'permission_denied', 'not_found', 'revision_not_found'}:
                    continue
                raise
            items.append(record)
        return HandlerOutput(data={'items': items})

    finish()
