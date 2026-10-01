"""Task identities use client-held keys and the existing live delegation chain."""

from datetime import timedelta
from uuid import uuid4

from msg.constants import PUBLIC_GROUP, ROOT_SUBJECT
from msg.core.codec import b64, canonical, decode, parse_time, unb64, wire
from msg.core.errors import require
from msg.core.models import (
    CapabilityGrant,
    Credential,
    HandlerOutput,
    Resource,
    ResourceRef,
    Signature,
    Subject,
)
from msg.plugins.common import create_resource, operation_id, resolve
from msg.plugins.schemas import BYTES, GRANTS, IDENTIFIER, SIGNATURE, STRING, obj
from msg.security.age_keys import encryption_key_id, public_from_recipient
from msg.security.crypto import key_id, subject_id, verify
from msg.security.policy import constraints_subset, scope_subset


def possession_body(service, public_key, recipient, grantor):
    return canonical({
        'target_service': service,
        'public_key': public_key,
        'encryption_recipient': recipient,
        'grantor': grantor,
    })


async def covers(grants, requested, tx):
    return all([
        any([
            g.capability == r.capability
            and g.version == r.version
            and r.operations <= g.operations
            and await scope_subset(r.scope, g.scope, tx)
            and constraints_subset(r.constraints, g.constraints)
            for g in grants
        ])
        for r in requested
    ])


def install(app, op):
    @op(
        'identity.delegated_create',
        obj(
            {
                'grantor': STRING,
                'public_key': BYTES,
                'encryption_recipient': STRING,
                'possession_proof': SIGNATURE,
                'grants': GRANTS,
                'ttl': {'type': 'integer', 'minimum': 1, 'maximum': 86400},
                'depth': {'type': 'integer', 'minimum': 0, 'maximum': 8},
                'max_uses': {'type': 'integer', 'minimum': 1, 'maximum': 100000},
            },
            ('grantor', 'public_key', 'encryption_recipient', 'possession_proof', 'grants', 'ttl'),
        ),
        signature=True,
    )
    async def create(ctx, request, tx):
        from msg.plugins.identity import issue_online

        p, a = ctx.principal, request.arguments
        require(p.subject is not None and p.subject != ROOT_SUBJECT, 'local_only')
        owner = await tx.resource(p.subject)
        require(a['grantor'] in {p.subject, owner.name}, 'delegated_grantor_mismatch')
        issuer = await app.online_issuer(tx)
        public = unb64(a['public_key'], limit=32)
        require(len(public) == 32, 'invalid_public_key')
        recipient = a['encryption_recipient']
        age_public = public_from_recipient(recipient)
        require(public != age_public, 'encryption_key_must_be_independent')
        verify(
            public,
            possession_body(app.settings.service_url, a['public_key'], recipient, a['grantor']),
            decode(Signature, a['possession_proof']),
            purpose='delegated-identity-v1',
        )
        uid, kid = subject_id(public), key_id(public)
        require(tx.one('SELECT id FROM credentials WHERE id=?', (kid,)) is None, 'key_exists')
        require(tx.one('SELECT id FROM resources WHERE id=?', (uid,)) is None, 'identity_exists')
        grants = tuple(decode(CapabilityGrant, g) for g in a['grants'])
        require(bool(grants), 'delegation_grants_required')
        require(await covers(p.ceiling, grants, tx), 'credential_ceiling_escalation')
        for grant in grants:
            require(grant.capability in app.base_capability_names, 'special_delegation_requires_ca')
            await app.certificates.validate_grant(grant, tx)
            resource = await tx.resource(grant.scope.resource_id)
            require(resource.owner == p.subject, 'delegation_owner_required')
            await app.authorizer._ceiling(p, operation_id(request), resource.id, tx)
            for operation in grant.operations:
                await app.authorizer._ceiling(p, operation, resource.id, tx)
        depth = a.get('depth', 0)
        if 'max_uses' in a:
            require(depth == 0, 'limited_delegation_depth')
            for grant in grants:
                for operation in grant.operations:
                    name, version = operation.rsplit('@', 1)
                    spec = app.registry.operation(name, int(version))
                    require(
                        spec.effect == 'transaction'
                        and not name.startswith('batch.')
                        and name != 'file.batch'
                        and not name.startswith('identity.'),
                        'limited_delegation_operation',
                    )
        expires = ctx.now + timedelta(seconds=a['ttl'])
        credential = await tx.credential(p.credential_id)
        require(
            credential.expires_at is None or expires <= credential.expires_at,
            'delegation_ttl_escalation',
        )
        require(a['ttl'] <= issuer.issuance.max_cert_ttl_seconds, 'certificate_ttl_escalation')
        parent = None
        if p.actor != p.subject:
            for cid in p.certificates:
                candidate = await app.certificates.validate(cid, tx)
                if (
                    candidate.kind == 'delegation'
                    and candidate.delegation_depth > depth
                    and expires <= candidate.expires_at
                    and await covers(candidate.grants, grants, tx)
                    and any(
                        (tx.setting('delegation:' + s.id) or {}).get('grantor') == p.subject
                        for s in candidate.authority_sources
                    )
                ):
                    parent = candidate
                    break
            require(parent is not None, 'redelegation_forbidden')
        expires = min(expires, issuer.expires_at)
        # Full random suffixes are never reused; a short suffix is only an example.
        address = owner.name + '~' + uuid4().hex
        user = Resource(
            id=uid,
            type='user',
            type_version=1,
            name=address,
            parent=app.namespace_root,
            owner=uid,
            group=PUBLIC_GROUP,
            mode=0o755,
            generation=0,
            revision=None,
            state='active',
            created_at=ctx.now,
            created_by=p.actor,
            modified_at=ctx.now,
            modified_by=p.actor,
        )
        await tx.insert(user)
        await tx.update_identity(
            Subject(resource_id=uid, kind='temporary', primary_group=PUBLIC_GROUP, auth_version=0),
            -1,
        )
        await tx.save_credential(
            Credential(
                id=kid,
                subject_id=uid,
                kind='signing_key',
                verifier=public,
                ceiling=grants,
                not_before=ctx.now,
                expires_at=expires,
                revoked_at=None,
            ),
            0,
        )
        tx.execute(
            'INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
            (kid, uid, b64(public), wire(ctx.now), None, 1),
            write=True,
        )
        age_id = encryption_key_id(age_public)
        require(
            tx.one('SELECT key_id FROM encryption_subkeys WHERE key_id=?', (age_id,)) is None,
            'encryption_key_exists',
        )
        tx.execute(
            'INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
            (age_id, uid, recipient, b64(age_public), wire(ctx.now), None, 1),
            write=True,
        )
        fact = {
            'grantor': p.subject,
            'grantee': uid,
            'grants': wire(grants),
            'expires_at': wire(expires),
            'parent_certificate': parent.resource_id if parent else None,
            'grantor_credential_id': credential.id,
        }
        delegation = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent=p.subject,
            type='delegation',
            name='delegation_' + uuid4().hex,
            body=canonical(fact),
            media_type='application/json',
            mode=0o600,
        )
        tx.set_setting('delegation:' + delegation.id, fact)
        cert = await issue_online(
            app,
            tx,
            uid,
            kid,
            ctx,
            request,
            grants=grants,
            kind='delegation',
            sources=(ResourceRef(id=delegation.id, revision=delegation.revision),),
            depth=depth,
            ttl=(expires - ctx.now).total_seconds(),
        )
        binding = {
            'grantor': p.subject,
            'grantor_address': owner.name,
            'delegation_id': delegation.id,
            'certificate_id': cert.resource_id,
            'expires_at': wire(expires),
            'address': address,
            'max_uses': a.get('max_uses'),
            'uses': 0,
        }
        tx.set_setting('delegated_identity:' + uid, binding)
        return HandlerOutput(
            resources=(ResourceRef(id=delegation.id),),
            data={
                **binding,
                'subject_id': uid,
                'key_id': kid,
                'encryption_key_id': age_id,
                'encryption_recipient': recipient,
                'target_service': app.settings.service_url,
                'grants': wire(grants),
                'depth': depth,
            },
        )

    @op('identity.delegated_get', obj({'id': IDENTIFIER}, ('id',)), effect='read', signature=True)
    async def get(ctx, request, tx):
        uid = await resolve(tx, request.arguments['id'])
        binding = tx.setting('delegated_identity:' + uid)
        require(binding is not None, 'not_delegated_identity')
        require(
            ctx.principal.actor == ctx.principal.subject == binding['grantor'],
            'identity_owner_required',
        )
        await app.authorizer.require_base(
            ctx.principal, operation_id(request), binding['grantor'], tx
        )
        delegation = await tx.resource(binding['delegation_id'])
        active = delegation.state == 'active' and ctx.now < parse_time(binding['expires_at'])
        if binding.get('max_uses') is not None:
            active = active and binding['uses'] < binding['max_uses']
        if active:
            from msg.core.errors import Failure

            try:
                await app.certificates.validate(binding['certificate_id'], tx)
            except Failure as exc:
                if exc.code in {
                    'authority_source_inactive',
                    'authority_source_expired',
                    'certificate_revoked',
                    'certificate_expired',
                    'authority_source_lost',
                }:
                    active = False
                else:
                    raise
        return HandlerOutput(data={**binding, 'subject_id': uid, 'active': active})
