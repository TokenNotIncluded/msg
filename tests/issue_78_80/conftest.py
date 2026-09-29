"""Isolated real Registry/Authorizer/Git with the supported transactional fake.

This does not replace production PostgreSQL, transport, or deployment tests.
It deliberately does not import the production Application or mock dependencies.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest_asyncio

from msg.constants import ROOT_SPACE
from msg.core.codec import canonical
from msg.core.cursors import CursorCodec
from msg.core.models import (
    Credential,
    ExecutionContext,
    PluginManifest,
    Principal,
    Resource,
    ResourceTypeSpec,
    Subject,
)
from msg.core.registry import Registry
from msg.core.requests import request_for
from msg.extensions import repositories
from msg.plugins import content, discovery, transfer
from msg.plugins.common import registration, revise_resource
from msg.security.authorization import AuthorizationService
from msg.security.capabilities import base_grants, install_capabilities
from msg.security.crypto import Ed25519Signer
from msg.storage.git import GitContentStore
from msg.storage.sqlite import FakeMetadataStore


@dataclass
class MutableLimits:
    max_request_bytes: int = 1048576
    max_response_bytes: int = 1048576
    max_path_bytes: int = 8192
    encodings: frozenset[str] = frozenset({'j', 'gz'})


@pytest_asyncio.fixture
async def harness(tmp_path):
    registry = Registry()
    types = tuple(
        ResourceTypeSpec(
            name=name,
            version=1,
            container=name in {'topic', 'user', 'organization', 'website', 'repo'},
            content_schema=None,
            operations=frozenset(),
            relations=frozenset(),
        )
        for name in (
            'topic',
            'post',
            'file',
            'attachment',
            'user',
            'organization',
            'website',
            'repo',
            'template',
            'tool',
            'skill',
        )
    )
    registry.add(
        PluginManifest(
            name='identity',
            version='1',
            dependencies=(),
            resource_types=types,
            capabilities=(),
            operations=(),
            migrations=(),
        )
    )
    metadata = FakeMetadataStore()
    app = SimpleNamespace(
        registry=registry,
        metadata=metadata,
        contents=GitContentStore(tmp_path / 'content'),
        cursors=CursorCodec(b'unit-test-only-cursor-key'),
        settings=SimpleNamespace(
            service_url='https://unit.invalid', server=SimpleNamespace(limits=MutableLimits())
        ),
    )
    content.install(app)
    discovery.install(app)
    transfer.install(app)
    app.settings.transfer_ttl = 900
    app.settings.max_part_bytes = 4096
    app.settings.server.blob_dir = app.contents.binary
    app.settings.server.repositories_dir = tmp_path / 'repositories'
    app.settings.server.staging_dir = app.contents.staging
    op, finish = registration(app, 'repositories', ('content',))
    repositories.register(app, op)
    finish()
    install_capabilities(registry)
    registry.freeze()
    # No certificate in this fixture. Unexpected certificate validation fails
    # rather than silently approving a fabricated certificate.
    app.authorizer = AuthorizationService(registry, None)
    signer = Ed25519Signer.generate()
    now = datetime.now(UTC)
    principal = Principal(
        actor='u_alice',
        subject='u_alice',
        credential_id=signer.key_id,
        method='signature',
        certificates=(),
        ceiling=base_grants(registry),
    )
    ctx = ExecutionContext(
        request_id='fixture',
        principal=principal,
        entry='network',
        now=now,
        deadline_monotonic=time.monotonic() + 120,
    )
    app.clock = lambda: now

    async def create(
        rid,
        parent=ROOT_SPACE,
        *,
        type='topic',
        name=None,
        body=None,
        mode=0o755,
        owner='u_alice',
        relations=(),
    ):
        resource = Resource(
            id=rid,
            type=type,
            type_version=1,
            name=name or rid,
            parent=parent,
            owner=owner,
            group='g_public',
            mode=mode,
            generation=0,
            revision=None,
            state='active',
            created_at=now,
            created_by=owner,
            modified_at=now,
            modified_by=owner,
        )
        async with metadata.transaction(write=True) as tx:
            await tx.insert(resource)
            if body is not None:
                request = request_for('content.file_put', {}, app.settings.service_url)
                resource = await revise_resource(
                    app, ctx, request, tx, resource, body, relations=relations
                )
        return resource

    async def invoke(operation, args, version=1, *, expected=(), context=None):
        request = request_for(
            operation,
            args,
            app.settings.service_url,
            subject='u_alice',
            signer=signer,
            contract_version=version,
            expected=expected,
        )
        spec = registry.operation(operation, version)
        registry.validate(spec.input_schema, args)
        actual = context or ctx
        async with metadata.transaction(write=spec.effect != 'read') as tx:
            requirements = await spec.requirements(request, tx)
            await app.authorizer.require(actual, request, requirements, tx)
            return await spec.handler(actual, request, tx)

    await create(ROOT_SPACE, None, name='root')
    await create('u_alice', type='user', name='@alice')
    subject = Subject(
        resource_id='u_alice',
        kind='registered',
        primary_group='g_public',
        auth_version=1,
        local_only=False,
    )
    credential = Credential(
        id=signer.key_id,
        subject_id='u_alice',
        kind='signing_key',
        verifier=signer.public_key,
        ceiling=principal.ceiling,
        not_before=now - timedelta(seconds=1),
        expires_at=None,
        revoked_at=None,
    )
    async with metadata.transaction(write=True) as tx:
        tx.execute(
            'INSERT INTO identities VALUES (?,?,?,?)',
            ('u_alice', 'subject', 1, canonical(subject).decode()),
            write=True,
        )
        tx.execute(
            'INSERT INTO credentials VALUES (?,?,?)',
            (credential.id, 'u_alice', canonical(credential).decode()),
            write=True,
        )
    # Exercise real request authentication and executor paths without the
    # production Application's mandatory PostgreSQL/Valkey services.
    from msg.core.executor import OperationExecutor
    from msg.core.models import Certificate, Signature
    from msg.security.authentication import AuthenticationService
    from msg.security.certificates import CertificateValidator, sign_certificate

    root = Ed25519Signer.generate()
    certificate = Certificate(
        resource_id='cert_test',
        serial='test-only',
        subject_id='u_root',
        key_id=root.key_id,
        issuer_id='u_root',
        parent_certificate_id=None,
        authority_sources=(),
        kind='ca',
        grants=(),
        not_before=now - timedelta(seconds=1),
        expires_at=now + timedelta(days=1),
        target_service=app.settings.service_url,
        delegation_depth=0,
        issuance=None,
        signature=Signature(key_id=root.key_id, algorithm='ed25519', value=b''),
    )
    app.certificates = CertificateValidator(
        registry,
        sign_certificate(certificate, root),
        root.public_key,
        app.settings.service_url,
        app.clock,
    )
    app.authorizer = AuthorizationService(registry, app.certificates)
    app.authenticator = AuthenticationService(
        registry,
        app.certificates,
        app.settings.service_url,
        app.clock,
        lambda: base_grants(registry),
        lambda: base_grants(registry),
    )

    async def result_projection(context, request, session, resource, *, fields):
        return await discovery.read_projection(
            app, context, request, session, resource.id, revision=resource.revision, fields=fields
        )

    app.executor = OperationExecutor(
        registry,
        metadata,
        app.contents,
        app.authenticator,
        app.authorizer,
        app.clock,
        Ed25519Signer.generate(),
        max_request_bytes=app.settings.server.limits.max_request_bytes,
        result_projection=result_projection,
    )
    app._loaded = True

    def packet(operation, args, version=1, *, signed=True, **kwargs):
        return request_for(
            operation,
            args,
            app.settings.service_url,
            subject='u_alice' if signed else None,
            signer=signer if signed else None,
            contract_version=version,
            expires_at=now + timedelta(seconds=30),
            **kwargs,
        )

    async def execute(operation, args, version=1, **kwargs):
        return await app.executor.execute(packet(operation, args, version, **kwargs))

    yield SimpleNamespace(
        app=app,
        ctx=ctx,
        signer=signer,
        create=create,
        invoke=invoke,
        packet=packet,
        execute=execute,
    )

    await metadata.close()
