"""Explicit resource use cases shared by content and extensions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace as replace
from uuid import uuid4 as uuid4

from msg.constants import (
    ADMINS_GROUP as ADMINS_GROUP,
    CA_SPACE as CA_SPACE,
    CERT_SPACE as CERT_SPACE,
    CSR_SPACE as CSR_SPACE,
    ONLINE_CA as ONLINE_CA,
    PROTOCOL_VERSION as PROTOCOL_VERSION,
    PUBLIC_GROUP as PUBLIC_GROUP,
    ROOT_SPACE as ROOT_SPACE,
    ROOT_SUBJECT as ROOT_SUBJECT,
    TOOLS_SPACE as TOOLS_SPACE,
)
from msg.core.codec import (
    canonical as canonical,
    decode as decode,
    digest as digest,
    loads as loads,
    parse_time as parse_time,
    wire as wire,
)
from msg.core.errors import Failure as Failure, require as require
from msg.core.models import (
    AccessRequirement as AccessRequirement,
    HandlerOutput as HandlerOutput,
    Resource as Resource,
    ResourceRef as ResourceRef,
    Revision as Revision,
    Signature as Signature,
)
from msg.security.crypto import verify as verify
from msg.security.policy import SETGID as SETGID, inherit_group as inherit_group


def new_id(prefix='r'):
    return prefix + '_' + uuid4().hex


def operation_id(request):
    return f'{request.operation}@{request.contract_version}'


async def resolve(session, value):
    if isinstance(value, str) and value.startswith('/'):
        return await session.resolve(value)
    require(
        isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value)),
        'invalid_resource_id',
    )
    return (await session.resource(value)).id


async def resolve_read(session, value):
    """Read-only migration lookup; authorization still uses the current object."""
    try:
        return await resolve(session, value)
    except Failure as exc:
        if exc.code != 'not_found' or not isinstance(value, str) or not value.startswith('/'):
            raise
        return await session.resolve_migrated(value)


async def check_access(app, context, request, session, rid, check):
    return await app.authorizer.require(
        context,
        request,
        (AccessRequirement(resource_id=rid, operation=operation_id(request), check=check),),
        session,
    )


def requirement(argument, check, *, default=None):
    async def requirements(request, session):
        target = request.arguments.get(argument, default)
        rid = await resolve(session, target)
        return (AccessRequirement(resource_id=rid, operation=operation_id(request), check=check),)

    return requirements


async def no_requirements(request, session):
    return ()


def validate_name(name, *, identity=False):
    require(
        isinstance(name, str)
        and 1 <= len(name) <= 120
        and '/' not in name
        and '\\' not in name
        and name not in {'.', '..', 'json', 'meta', 'raw', 'history', 'revisions'}
        and all(ord(c) >= 32 and ord(c) != 127 for c in name),
        'invalid_name',
    )
    if not identity:
        require(not name.startswith(('@', '&', '!', '~')), 'reserved_name')
    return name


async def protect_namespace(app, ctx, request, tx, parent, name):
    if name.startswith('_'):
        require(
            await app.authorizer.has(
                ctx.principal, 'system.namespace', operation_id(request), parent.id, tx
            ),
            'protected_namespace',
        )


SUMMARY_UNSET = object()
MAX_SUMMARY_CHARS = 280


async def create_resource(
    app,
    ctx,
    request,
    tx,
    *,
    parent,
    type,
    name=None,
    body=None,
    media_type='text/markdown',
    relations=(),
    mode=None,
    resource_id=None,
    author=None,
    content_signature=None,
    revision_id=None,
    summary=None,
):
    parent = await tx.resource(parent)
    require(
        type not in {'order', 'order_collection'} and parent.type != 'order_collection',
        'order_controlled_resource',
    )
    require(parent.state == 'active', 'ancestor_inactive')
    require(
        app.registry.resource_type(parent.type, parent.type_version).container, 'not_a_container'
    )
    app.registry.resource_type(type, 1)
    if parent.id == 't_store' or type == 'listing':
        require(
            parent.id == 't_store'
            and type == 'listing'
            and request.operation in {'store.listing_create', 'bounty.create'},
            'store_controlled_resource',
        )
    if parent.id == 't_last_will':
        require(
            request.operation == 'identity.legacy_put' and type == 'legacy_directive',
            'legacy_directive_only',
        )
    if type == 'post':
        name = name or new_id('p')
        if not name.endswith('.md'):
            name += '.md'
    name = validate_name(name or new_id('p'))
    if parent.type == 'user' and name in {'SOUL.md', 'AGENTS.md', 'notes', 'todos'}:
        require(
            request.operation
            in {'identity.personal_put', 'identity.note_put', 'identity.todo_put'},
            'personal_managed_resource',
        )
    if (
        parent.name == 'notes'
        and parent.parent is not None
        and (await tx.resource(parent.parent)).type == 'user'
    ):
        require(request.operation == 'identity.note_put', 'personal_managed_resource')
    if (
        parent.name == 'todos'
        and parent.parent is not None
        and (await tx.resource(parent.parent)).type == 'user'
    ):
        require(
            request.operation == 'identity.todo_put' and type == 'todo', 'personal_managed_resource'
        )
    await protect_namespace(app, ctx, request, tx, parent, name)
    principal = ctx.principal
    subject = await tx.subject(principal.subject)
    policy = tx.setting('policy:' + parent.id, {})
    container = app.registry.resource_type(type, 1).container
    default = policy.get(
        type + '_mode',
        policy.get('topic_mode' if container else 'post_mode', '1777' if container else '0644'),
    )
    mode = int(default, 8) if mode is None else mode
    if container and parent.mode & SETGID:
        mode |= SETGID
    from msg.core.wiki import in_wiki

    if await in_wiki(tx, parent):
        require(type in {'post', 'topic', 'file', 'attachment'}, 'wiki_content_only')
        mode = 0o1777 if container else 0o666
    rid = resource_id or new_id()
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', rid)), 'invalid_resource_id')
    resource = Resource(
        id=rid,
        type=type,
        type_version=1,
        name=name,
        parent=parent.id,
        owner=subject.resource_id,
        group=inherit_group(parent, subject.primary_group),
        mode=mode,
        generation=0,
        revision=None,
        state='active',
        created_at=ctx.now,
        created_by=principal.actor,
        modified_at=ctx.now,
        modified_by=principal.actor,
    )
    await tx.insert(resource)
    if body is not None:
        resource = await revise_resource(
            app,
            ctx,
            request,
            tx,
            resource,
            body,
            media_type,
            relations=relations,
            author=author,
            signature=content_signature,
            revision_id=revision_id,
            summary=summary,
        )
    return resource


async def revise_resource(
    app,
    ctx,
    request,
    tx,
    resource,
    body,
    media_type='text/markdown',
    *,
    relations=(),
    author=None,
    signature=None,
    revision_id=None,
    change_note=None,
    source_kind=None,
    source_version=None,
    source_digest=None,
    content_created_at=None,
    summary=SUMMARY_UNSET,
):
    require(resource.type not in {'order', 'order_collection'}, 'order_controlled_resource')
    from msg.core.models import BlobRef

    # Internal operation labels (e.g. transfer.seal publishing) carry no client arguments.
    timestamp = (
        content_created_at
        if content_created_at is not None
        else getattr(request, 'arguments', {}).get('content_created_at')
    )
    # A client-chosen Revision ID or content time is only defined as part of the
    # signed manifest; without the signature it would be an unproven claim.
    require(
        signature is not None or (revision_id is None and timestamp is None),
        'content_signature_required',
    )
    require(signature is None or revision_id is not None, 'revision_id_required')
    if isinstance(body, BlobRef):
        blob = body
        # A retained index/reference is not evidence the payload survived.
        # Verify copies and reused blobs before publishing another Revision.
        hasher = hashlib.sha256()
        size = 0
        try:
            async for chunk in app.contents.read(blob):
                size += len(chunk)
                hasher.update(chunk)
        except FileNotFoundError as exc:
            raise Failure('content_missing') from exc
        require(
            size == blob.size and 'sha256:' + hasher.hexdigest() == blob.digest,
            'content_digest_mismatch',
        )
    else:
        blob = await app.contents.put_bytes(
            body.encode('utf-8') if isinstance(body, str) else body, media_type
        )
    for relation in relations:
        spec = app.registry.resource_type(resource.type, resource.type_version)
        require(relation.type in spec.relations, 'invalid_relation_type')
        await check_access(app, ctx, request, tx, relation.target.id, 'read')
        if relation.target.revision is not None:
            await tx.revision(relation.target)
    rid = revision_id or new_id('v')
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', rid)), 'invalid_revision_id')
    content_time = ctx.now
    if signature is not None:
        require(timestamp is not None, 'content_timestamp_required')
        content_time = parse_time(timestamp)
        require(
            abs((content_time - ctx.now).total_seconds()) <= 300, 'content_timestamp_out_of_range'
        )
    if summary is SUMMARY_UNSET:
        summary = (
            (await tx.revision(ResourceRef(id=resource.id))).summary
            if resource.type == 'post' and resource.revision
            else None
        )
    require(
        summary is None or (isinstance(summary, str) and len(summary) <= MAX_SUMMARY_CHARS),
        'invalid_summary',
    )
    summary = summary or None
    revision = Revision(
        format_version=1,
        id=rid,
        resource_id=resource.id,
        parents=(resource.revision,) if resource.revision else (),
        content=blob,
        relations=tuple(relations),
        actor=ctx.principal.actor,
        subject=ctx.principal.subject,
        author=author or ctx.principal.subject,
        created_at=content_time,
        manifest_digest='',
        summary=summary,
        change_note=change_note,
        source_kind=source_kind,
        source_version=source_version,
        source_digest=source_digest,
    )
    custodial = (
        ctx.principal.method == 'token'
        and (await tx.subject(ctx.principal.subject)).kind == 'custodial'
    )
    if custodial:
        require(signature is None, 'custodial_content_signature_forbidden')
        revision = replace(
            revision,
            signature_source='custodial',
            source_kind='operation',
            source_version=request.contract_version,
            source_digest=request.payload_digest,
        )
    body_to_sign = {
        k: v for k, v in wire(revision).items() if k not in {'manifest_digest', 'signature'}
    }
    revision = replace(revision, manifest_digest=digest(body_to_sign))
    if signature is not None:
        sig = decode(Signature, signature)
        credential = await tx.credential(ctx.principal.credential_id)
        require(sig.key_id == credential.id, 'content_signer_mismatch')
        verify(credential.verifier, canonical(body_to_sign), sig, purpose='revision')
        revision = replace(revision, signature=sig)
    elif custodial:
        from msg.security.vault import open_signer

        signer = open_signer(app, tx, ctx.principal.subject)
        primary = tx.one(
            'SELECT key_id FROM identity_keys WHERE subject=? AND is_primary=1',
            (ctx.principal.subject,),
        )
        require(primary is not None and primary[0] == signer.key_id, 'custodial_vault_key_mismatch')
        revision = replace(
            revision, signature=signer.sign(canonical(body_to_sign), purpose='revision')
        )
    if revision_id is None or not await app.contents.pinned(blob, rid):
        tx.on_rollback(lambda: app.contents.unpin(blob, rid))
    await app.contents.pin(blob, rid)
    ancestors = await tx.ancestors(resource.id)
    topic = next((p.id for p in reversed(ancestors) if p.type == 'topic'), ROOT_SPACE)
    await app.contents.commit_revision(topic, revision)
    await tx.append_revision(revision)
    updated = replace(
        resource,
        generation=resource.generation + 1,
        revision=rid,
        modified_at=ctx.now,
        modified_by=ctx.principal.actor,
    )
    await tx.replace(updated, resource.generation)
    if media_type.startswith('text/') and blob.size <= 1048576:
        text = (await app.contents.read_bytes(blob)).decode('utf-8', errors='replace')
        tx.execute(
            'INSERT INTO projections VALUES (?,?) ON CONFLICT(resource_id) DO UPDATE SET text=excluded.text',
            (resource.id, text),
            write=True,
        )
    else:
        tx.execute('DELETE FROM projections WHERE resource_id=?', (resource.id,), write=True)
    return updated


def output_for(resource, **data):
    return HandlerOutput(
        resources=(ResourceRef(id=resource.id, revision=resource.revision),),
        data={'generation': resource.generation, **data},
    )


async def assert_generation(request, resource):
    values = dict(request.expected_generations)
    require(resource.id in values, 'expected_generation_required')
    require(
        values[resource.id] == resource.generation,
        'generation_conflict',
        details={
            'id': resource.id,
            'generation': resource.generation,
            'revision': resource.revision,
        },
    )


async def topic_policy(tx, resource):
    topic = resource if resource.type == 'topic' else await tx.resource(resource.parent)
    return tx.setting('policy:' + topic.id, {})


def default_operation_rules(name):
    """Compatibility defaults captured when constructing an operation."""
    if name.startswith('identity.') or name in {
        'communication.follow',
        'communication.unfollow',
        'communication.agent_following',
        'communication.followers',
        'discovery.recommendations',
    }:
        rules = ('identity', 'auth')
    elif name.startswith(('content.topic_', 'discussion.')):
        rules = ('topics', 'read-write')
    elif name.startswith('content.'):
        rules = ('read-write',)
    elif name.startswith('file.'):
        rules = ('files', 'read-write', 'protocol')
    elif name.startswith(('transfer.', 'keystore.', 'git.')):
        rules = ('files', 'protocol')
    elif name.startswith(('cert.', 'group.')):
        rules = ('auth',)
    elif name.startswith(('system.', 'tool.')):
        rules = ('security', 'protocol')
    elif name.startswith('discovery.'):
        rules = ('read-write', 'protocol')
    else:
        rules = ('protocol',)
    return tuple('msg.' + rule for rule in rules)


def registration(app, name, dependencies=()):
    from msg.core.models import OperationSpec, PluginManifest, ResourceRef
    from msg.plugins.schemas import OUTPUT

    operations = []
    output_ref = ResourceRef(id='schema:operation-result')
    if output_ref.id not in app.registry._schemas:
        app.registry.add_schema(output_ref, OUTPUT)

    def operation(
        opname,
        schema,
        *,
        effect='transaction',
        requirements=no_requirements,
        signature=False,
        version=1,
        requires_rules=None,
        enabled=True,
        anonymous_only=False,
    ):
        def decorate(handler):
            ref = ResourceRef(id='schema:' + opname + ':' + str(version))
            app.registry.add_schema(ref, schema)
            operations.append(
                OperationSpec(
                    name=opname,
                    version=version,
                    input_schema=ref,
                    output_schema=output_ref,
                    effect=effect,
                    entries=frozenset({'network', 'worker'}),
                    require_signature=signature,
                    requirements=requirements,
                    handler=handler,
                    enabled=enabled,
                    anonymous_only=anonymous_only,
                    requires_rules=default_operation_rules(opname)
                    if requires_rules is None
                    else tuple(requires_rules),
                )
            )
            return handler

        return decorate

    def finish(resource_types=()):
        from msg.plugins.features import feature_ids

        manifest = PluginManifest(
            name=name,
            version='1',
            dependencies=tuple(dependencies),
            resource_types=tuple(resource_types),
            capabilities=(),
            operations=tuple(operations),
            migrations=(),
            feature_ids=feature_ids(name),
        )
        app.registry.add(manifest)

    return operation, finish
