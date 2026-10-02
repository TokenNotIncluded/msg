"""Explicit Root-approved repair of two ordinary social operations on one key.

The preview opens existing metadata read-only, without Application.load, schema
migration, Root unlock or sequence allocation. This is never a network operation.
"""

import argparse
import asyncio
import re
from dataclasses import replace

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, decode, digest, loads, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import AuditEvent, Certificate, Event, ResourceRef
from msg.plugins.common import new_id
from msg.security.policy import scope_contains
from msg.security.quarantine import RuntimeGeneration, require_live_authority

FOLLOW_OPERATIONS = frozenset({'communication.follow@1', 'communication.unfollow@1'})


def _require_unmarked(app):
    # Re-resolve the current/legacy marker at each check, including after PIN input.
    marker = app.settings.recovery_marker
    require(not marker.exists() and not marker.is_symlink(), 'recovery_quarantined')


async def _subject(tx, reference):
    require(type(reference) is str, 'invalid_follow_repair_subject')
    if reference.startswith('@'):
        require(
            re.fullmatch(r'@[A-Za-z0-9][A-Za-z0-9_-]{0,63}', reference),
            'invalid_follow_repair_subject',
        )
        return await tx.resolve('/' + reference)
    require(re.fullmatch(r'u_[A-Za-z0-9_-]{1,128}', reference), 'invalid_follow_repair_subject')
    return reference


async def _plan(app, tx, reference, key_id=None):
    require_live_authority(tx)
    subject_id = await _subject(tx, reference)
    user, subject = await tx.resource(subject_id), await tx.subject(subject_id)
    require(
        user.type == 'user'
        and user.state == 'active'
        and user.owner == subject_id
        and subject.kind == 'registered'
        and not subject.local_only
        and subject_id != ROOT_SUBJECT,
        'follow_repair_registered_account_required',
    )
    require(not tx.setting('identity_archived:' + subject_id), 'account_archived')
    require(not tx.setting('delegated_identity:' + subject_id), 'follow_repair_delegated_identity')
    row = tx.one(
        'SELECT key_id,public_key,retired_at FROM identity_keys WHERE subject=? AND is_primary=1',
        (subject_id,),
    )
    require(row is not None and row[2] is None, 'follow_repair_active_primary_required')
    require(key_id is None or row[0] == key_id, 'follow_repair_primary_key_mismatch')
    credential = await tx.credential(row[0])
    now = app.clock()
    require(
        credential.subject_id == subject_id
        and credential.kind == 'signing_key'
        and credential.source_credential_id is None
        and credential.verifier == unb64(row[1], limit=32),
        'follow_repair_signing_key_required',
    )
    require(credential.revoked_at is None, 'credential_revoked')
    require(credential.not_before <= now, 'credential_not_yet_valid')
    require(credential.expires_at is None or credential.expires_at > now, 'credential_expired')
    allowed = app.registry.capability('communication.basic', 1).operations
    require(FOLLOW_OPERATIONS <= allowed, 'follow_repair_operations_unavailable')
    for operation in FOLLOW_OPERATIONS:
        name, _, version = operation.partition('@')
        spec = app.registry.operation(name, int(version))
        require(
            spec.enabled and spec.effect == 'transaction' and not spec.anonymous_only,
            'follow_repair_operations_unavailable',
        )
    ceilings, scopes, current = [], [], set()
    for index, grant in enumerate(credential.ceiling):
        eligible = (
            grant.capability == 'communication.basic'
            and grant.version == 1
            and await scope_contains(grant.scope, subject_id, tx)
        )
        if eligible:
            current.update(grant.operations & FOLLOW_OPERATIONS)
            scopes.append({
                'index': index,
                'scope': wire(grant.scope),
                'constraints': wire(grant.constraints),
            })
            grant = replace(grant, operations=grant.operations | FOLLOW_OPERATIONS)
        ceilings.append(grant)
    require(scopes, 'follow_repair_existing_communication_grant_required')
    updated = replace(credential, ceiling=tuple(ceilings))
    additions = sorted({
        operation
        for before, after in zip(credential.ceiling, updated.ceiling, strict=True)
        for operation in after.operations - before.operations
    })
    plan = {
        'action': 'repair_follow_authority',
        'service': app.settings.service_url,
        'subject_id': subject_id,
        'handle': user.name,
        'key_id': credential.id,
        'auth_version': subject.auth_version,
        'before_credential_digest': digest(credential),
        'after_credential_digest': digest(updated),
        'current': sorted(current),
        'additions': additions,
        'scopes': scopes,
        'scope_and_constraints_unchanged': True,
        'root_signature_required': bool(additions),
    }
    return plan, subject, credential, updated


async def follow_preview(app, tx, subject, key_id=None):
    """Return public identifiers and two-operation differences, never key verifiers."""
    return (await _plan(app, tx, subject, key_id))[0]


async def preview_configuration(config_dir, subject, *, key_id=None):
    from msg.application import Application
    from msg.config import load_settings
    from msg.storage.postgres import PostgresMetadataStore

    app = Application(load_settings(config_dir))
    # No Application.load/open_storage: those can initialize and synchronize resources.
    store = PostgresMetadataStore(app.settings.server.postgres_dsn, initialize=False)
    try:
        async with store.transaction(write=False) as tx:
            plan = await follow_preview(app, tx, subject, key_id)
        return {'preview': plan, 'digest': digest(plan), 'applied': False, 'read_only': True}
    finally:
        await store.close()


async def open_for_repair(config_dir):
    """Open existing metadata and public trust, without migration or service keys."""
    from msg.application import Application
    from msg.config import load_settings
    from msg.security.backup_retirement import root_verifier
    from msg.security.certificates import CertificateValidator
    from msg.storage.postgres import PostgresMetadataStore

    app = Application(load_settings(config_dir))
    app.metadata = PostgresMetadataStore(app.settings.server.postgres_dsn, initialize=False)
    try:
        _require_unmarked(app)
        trust = loads(app.settings.trust_file.read_bytes())
        require(
            set(trust) == {'version', 'public_key', 'certificate'} and trust['version'] == 1,
            'invalid_trust_anchor',
        )
        root_public = unb64(trust['public_key'], limit=32)
        root_certificate = decode(Certificate, trust['certificate'])
        app.certificates = CertificateValidator(
            app.registry, root_certificate, root_public, app.settings.service_url, app.clock
        )
        async with app.metadata.transaction(write=False) as tx:
            require_live_authority(tx)
            app.runtime_generation = RuntimeGeneration.capture(tx)
            await app.certificates.validate(root_certificate.resource_id, tx)
            require(await root_verifier(tx, now=app.clock()) == root_public, 'root_key_mismatch')
            require((await tx.subject(ROOT_SUBJECT)).local_only, 'root_policy_corrupt')
        return app
    except BaseException:
        await app.close()
        raise


async def repair_follows(app, subject, signer, *, key_id=None, expected_digest, operator):
    from msg.security.backup_retirement import root_verifier

    require(signer.public_key == app.certificates.root_public_key, 'root_key_mismatch')
    require(type(operator) is str and bool(operator), 'follow_repair_operator_required')
    async with app.metadata.transaction(write=True) as tx:
        app.runtime_generation.require_current(tx)
        _require_unmarked(app)
        require(
            await root_verifier(tx, now=app.clock()) == signer.public_key,
            'root_key_mismatch',
        )
        plan, identity, before, updated = await _plan(app, tx, subject, key_id)
        require(digest(plan) == expected_digest, 'follow_repair_preview_changed')
        if not plan['additions']:
            return {
                'subject_id': identity.resource_id,
                'key_id': before.id,
                'changed': False,
                'additions': [],
            }
        now = app.clock()
        await tx.save_credential(updated, identity.auth_version)
        await tx.update_identity(
            replace(identity, auth_version=identity.auth_version + 1), identity.auth_version
        )
        tx.set_setting('authorization_epoch', tx.setting('authorization_epoch', 0) + 1)
        statement = {
            **plan,
            'operator': operator,
            'time': wire(now),
            'result_auth_version': identity.auth_version + 1,
        }
        signature = wire(signer.sign(canonical(statement), purpose='account-admin'))
        event = Event(
            id=new_id('audit'),
            type='root.identity.follow_authority_repair',
            time=now,
            request_id=new_id('local'),
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            resources=(ResourceRef(id=identity.resource_id),),
            data={**statement, 'signature': signature},
        )
        _require_unmarked(app)
        await tx.append_audit(
            AuditEvent(
                event=event,
                authority=(ResourceRef(id=app.certificates.root_certificate.resource_id),),
                before_digest=digest(before),
                after_digest=digest(updated),
                previous_digest=None,
                entry_digest='',
                result='repaired',
            )
        )
    return {
        'subject_id': identity.resource_id,
        'key_id': before.id,
        'changed': True,
        'additions': plan['additions'],
        'audit_event_id': event.id,
        'auth_version': identity.auth_version + 1,
    }


def main(argv=None):
    """A standalone read-only preview also works before the admin CLI is released."""
    parser = argparse.ArgumentParser(description='Read-only preview of ordinary follow permissions')
    parser.add_argument('--config-dir', required=True)
    parser.add_argument('subject')
    parser.add_argument('--key-id')
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(
            preview_configuration(args.config_dir, args.subject, key_id=args.key_id)
        )
    except Failure as exc:
        print(canonical({'status': 'error', 'error': {'code': exc.code}}).decode())
        return 1
    print(canonical(result).decode())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
