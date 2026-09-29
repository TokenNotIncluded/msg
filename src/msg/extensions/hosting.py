"""Explicit static publishing under the service origin with sandboxed responses."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from msg.core.codec import canonical, decode, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import ResourceRef
from msg.core.requests import request_for
from msg.plugins.common import (
    assert_generation,
    check_access,
    create_resource,
    new_id,
    output_for,
    resolve,
    resolve_read,
    revise_resource,
)
from msg.plugins.schemas import IDENTIFIER, REF, STRING, obj

MAX_HOSTING_ENTRIES = 128


def path_name(value):
    require(
        isinstance(value, str)
        and 0 < len(value) <= 2048
        and not value.startswith('/')
        and '\\' not in value
        and all(ord(c) >= 32 and ord(c) != 127 for c in value),
        'invalid_hosting_path',
    )
    parts = value.split('/')
    require(
        all(part not in {'', '.', '..'} and not part.startswith('.') for part in parts),
        'invalid_hosting_path',
    )
    return '/'.join(parts)


def register(app, op):
    @op(
        'hosting.create',
        obj({'parent': IDENTIFIER, 'name': STRING}, ('parent', 'name')),
        signature=True,
    )
    async def create(ctx, request, tx):
        parent = await resolve(tx, request.arguments['parent'])
        require(
            (await tx.resource(parent)).type in {'user', 'organization'},
            'hosting_namespace_required',
        )
        await check_access(app, ctx, request, tx, parent, 'create')
        require(
            all(r.mode & 1 for r in [await tx.resource(parent), *await tx.ancestors(parent)]),
            'hosting_public_path_required',
        )
        resource = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent=parent,
            type='website',
            name=request.arguments['name'],
            mode=0o755,
        )
        return output_for(
            resource, url=app.settings.service_url.rstrip('/') + await tx.path(resource.id) + '/'
        )

    @op(
        'hosting.deploy',
        obj(
            {
                'id': IDENTIFIER,
                'entries': {
                    'type': 'array',
                    'minItems': 1,
                    'items': obj({'path': STRING, 'source': REF}, ('path', 'source')),
                },
            },
            ('id', 'entries'),
        ),
        signature=True,
    )
    async def deploy(ctx, request, tx):
        resource = await tx.resource(await resolve(tx, request.arguments['id']))
        require(resource.type == 'website' and resource.state == 'active', 'not_a_website')
        await check_access(app, ctx, request, tx, resource.id, 'write')
        await assert_generation(request, resource)
        # A deployment must stay within what a preview could have shown first.
        require(
            len(request.arguments['entries']) <= MAX_HOSTING_ENTRIES, 'too_many_hosting_entries'
        )
        sources = []
        names = set()
        for item in request.arguments['entries']:
            path = path_name(item['path'])
            require(path not in names, 'duplicate_hosting_path')
            names.add(path)
            ref = decode(ResourceRef, item['source'])
            require(ref.revision is not None, 'source_revision_required')
            await check_access(app, ctx, request, tx, ref.id, 'read')
            revision = await tx.revision(ref)
            sources.append((path, ref, revision.content))
        from msg.plugins.hosting_capacity import require_capacity

        await require_capacity(app, tx, resource, sum(blob.size for _, _, blob in sources), ctx.now)
        # Public copies share CAS bytes but get their own explicit publication
        # facts and ACL. A later source chmod does not secretly unpublish a site.
        deployment = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent=resource.id,
            type='topic',
            name='deploy-' + new_id('d'),
            mode=0o755,
        )
        entries = {}
        for index, (path, source, blob) in enumerate(sources):
            published = await create_resource(
                app,
                ctx,
                request,
                tx,
                parent=deployment.id,
                type='file',
                name=f'file-{index}',
                body=blob,
                media_type=blob.media_type,
                mode=0o644,
            )
            tx.set_setting(
                'publication:' + published.id,
                {
                    'source': wire(source),
                    'website': resource.id,
                    'path': path,
                    'publisher': ctx.principal.subject,
                },
            )
            entries[path] = {'id': published.id, 'revision': published.revision}
        body = canonical({'format_version': 1, 'deployment': deployment.id, 'entries': entries})
        resource = await revise_resource(app, ctx, request, tx, resource, body, 'application/json')
        return output_for(
            resource,
            files=len(entries),
            url=app.settings.service_url.rstrip('/') + await tx.path(resource.id) + '/',
        )

    @op(
        'hosting.preview',
        obj(
            {
                'id': IDENTIFIER,
                'entries': {
                    'type': 'array',
                    'minItems': 1,
                    'items': obj({'path': STRING, 'source': REF}, ('path', 'source')),
                },
            },
            ('id', 'entries'),
        ),
        signature=True,
    )
    async def preview(ctx, request, tx):
        website = await tx.resource(await resolve(tx, request.arguments['id']))
        require(website.type == 'website' and website.state == 'active', 'not_a_website')
        await check_access(app, ctx, request, tx, website.id, 'write')
        await assert_generation(request, website)
        # Keep the published v1 wire schema immutable; bound work before any
        # source traversal or materialization, just like runtime capacity checks.
        require(
            len(request.arguments['entries']) <= MAX_HOSTING_ENTRIES, 'too_many_preview_entries'
        )
        sources = []
        names = set()
        for item in request.arguments['entries']:
            path = path_name(item['path'])
            require(path not in names, 'duplicate_hosting_path')
            names.add(path)
            ref = decode(ResourceRef, item['source'])
            require(ref.revision is not None, 'source_revision_required')
            await check_access(app, ctx, request, tx, ref.id, 'read')
            sources.append((path, (await tx.revision(ref)).content))
        from msg.plugins.hosting_capacity import require_capacity

        await require_capacity(app, tx, website, sum(blob.size for _, blob in sources), ctx.now)
        candidate = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent=website.id,
            type='topic',
            name='preview-' + new_id('p'),
            mode=0o700,
        )
        entries = {}
        for index, (path, blob) in enumerate(sources):
            private = await create_resource(
                app,
                ctx,
                request,
                tx,
                parent=candidate.id,
                type='file',
                name=f'file-{index}',
                body=blob,
                media_type=blob.media_type,
                mode=0o600,
            )
            tx.set_setting(
                'hosting_preview_file:' + private.id,
                {'website': website.id, 'owner': ctx.principal.subject},
            )
            entries[path] = {'id': private.id, 'revision': private.revision}
        candidate = await revise_resource(
            app,
            ctx,
            request,
            tx,
            candidate,
            canonical({'format_version': 1, 'entries': entries}),
            'application/json',
        )
        tx.set_setting(
            'hosting_preview:' + candidate.id,
            {'website': website.id, 'owner': ctx.principal.subject},
        )
        return output_for(
            candidate,
            files=len(entries),
            url=(
                app.settings.service_url.rstrip('/')
                + await tx.path(website.id)
                + '/_preview/'
                + candidate.id
                + '/'
            ),
        )

    @op(
        'hosting.activate',
        obj({'id': IDENTIFIER, 'revision': IDENTIFIER}, ('id', 'revision')),
        signature=True,
    )
    async def activate(ctx, request, tx):
        resource = await tx.resource(await resolve(tx, request.arguments['id']))
        require(resource.type == 'website', 'not_a_website')
        await check_access(app, ctx, request, tx, resource.id, 'write')
        await assert_generation(request, resource)
        revision = await tx.revision(
            ResourceRef(id=resource.id, revision=request.arguments['revision'])
        )
        # Validation binds the deployment to this website, not an arbitrary JSON file.
        value = loads(await app.contents.read_bytes(revision.content))
        require(
            (await tx.resource(value['deployment'])).parent == resource.id, 'deployment_mismatch'
        )
        from msg.plugins.hosting_capacity import manifest_size, require_capacity

        await require_capacity(
            app, tx, resource, await manifest_size(app, tx, resource, revision.id), ctx.now
        )
        changed = replace(
            resource,
            revision=revision.id,
            generation=resource.generation + 1,
            modified_at=ctx.now,
            modified_by=ctx.principal.actor,
        )
        await tx.replace(changed, resource.generation)
        return output_for(changed)


HOSTED_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    'Cache-Control': 'no-store',
}
DOWNLOAD_TYPES = {
    'application/xhtml+xml',
    'image/svg+xml',
    'application/xml',
    'text/xml',
    'application/pdf',
}


def hosted_error(code):
    status = (
        403
        if code
        in {
            'forbidden_host',
            'permission_denied',
            'credential_ceiling',
            'authentication_required',
            'local_only',
            'credential_revoked',
        }
        else 405
        if code == 'method_not_allowed'
        else 416
        if code == 'range_not_satisfiable'
        else 404
    )
    return Response(
        canonical({'error': code}),
        status_code=status,
        media_type='application/json',
        headers=HOSTED_HEADERS,
    )


def hosted_range(request, size, etag):
    requested = request.headers.get('range')
    if not requested or request.headers.get('if-range') not in {None, etag}:
        return (0, size), 200, {}
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
    require(match is not None and any(match.groups()), 'range_not_satisfiable')
    left, right = match.groups()
    if left:
        start = int(left)
        end = min(int(right) + 1, size) if right else size
    else:
        require(int(right) > 0, 'range_not_satisfiable')
        start = max(0, size - int(right))
        end = size
    require(0 <= start < end <= size, 'range_not_satisfiable')
    return (start, end), 206, {'Content-Range': f'bytes {start}-{end - 1}/{size}'}


async def serve_hosted(service, request):
    """Return None for ordinary resources; hosted requests use anonymous authority."""
    parts = request.url.path.strip('/').split('/')
    if len(parts) < 2 or not parts[0].startswith(('@', '&')):
        return None
    site_path = '/' + parts[0] + '/' + parts[1]
    try:
        async with service.metadata.transaction(write=False) as tx:
            from msg.security.quarantine import require_live_authority

            require_live_authority(tx)
            service.runtime_generation.require_current(tx)
            try:
                rid = await resolve_read(tx, site_path)
            except Failure as exc:
                if exc.code != 'not_found':
                    raise
                rid = None
            website = await tx.resource(rid) if rid is not None else None
            if website is None or website.type != 'website':
                return None
            raw_path = request.scope.get('raw_path', request.url.path.encode())
            try:
                require(
                    b'%' not in raw_path and raw_path.decode('ascii') == request.url.path,
                    'not_found',
                )
            except UnicodeDecodeError as exc:
                raise Failure('not_found') from exc
            require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
            require(website.state == 'active', 'not_found')
            file_parts = parts[2:]
            preview_id = None
            if len(file_parts) >= 2 and file_parts[0] == '_preview':
                preview_id = file_parts[1]
                require(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', preview_id) is not None, 'not_found')
                file_parts = file_parts[2:]
            revision_id = website.revision
            if preview_id is None and len(file_parts) >= 2 and file_parts[0] == '_rev':
                revision_id = file_parts[1]
                file_parts = file_parts[2:]
            file_path = '/'.join(file_parts) or 'index.html'
            if request.url.path.endswith('/') and file_parts:
                file_path += '/index.html'
            file_path = path_name(file_path)
            if preview_id is not None:
                from msg.transports.packet import path_packet

                signed = request.headers.get('x-msg-request')
                require(bool(signed), 'authentication_required')
                packet = path_packet(signed, 'j', service.settings.server.limits.max_request_bytes)
                require(
                    packet.operation == 'discovery.raw'
                    and dict(packet.arguments) == {'id': preview_id}
                    and packet.proof is not None,
                    'representation_mismatch',
                )
            else:
                require(revision_id is not None, 'not_found')
                packet = request_for('discovery.raw', {'id': rid}, service.settings.service_url)
            principal = await service.authenticator.authenticate(packet, tx, entry='network')
            import time

            from msg.core.models import ExecutionContext

            context = ExecutionContext(
                request_id=packet.request_id,
                principal=principal,
                entry='network',
                now=service.clock(),
                deadline_monotonic=time.monotonic() + 30,
            )
            await check_access(service, context, packet, tx, rid, 'read')
            if preview_id is not None:
                candidate = await tx.resource(preview_id)
                require(
                    candidate.type == 'topic'
                    and candidate.parent == rid
                    and candidate.state == 'active'
                    and candidate.revision is not None
                    and tx.setting('hosting_preview:' + candidate.id)
                    == {'website': rid, 'owner': candidate.owner},
                    'not_found',
                )
                await check_access(service, context, packet, tx, candidate.id, 'read')
                revision = await tx.revision(
                    ResourceRef(id=candidate.id, revision=candidate.revision)
                )
            else:
                revision = await tx.revision(ResourceRef(id=rid, revision=revision_id))
            manifest = loads(await service.contents.read_bytes(revision.content, limit=65536))
            require(
                type(manifest) is dict and type(manifest.get('entries')) is dict,
                'invalid_deployment',
            )
            item = manifest['entries'].get(file_path)
            require(item is not None, 'not_found')
            ref = decode(ResourceRef, item)
            await check_access(service, context, packet, tx, ref.id, 'read')
            blob = (await tx.revision(ref)).content
            canonical_site = await tx.path(rid)
            if canonical_site != site_path:
                # Keep the original file/revision/preview suffix. All current
                # website and leaf authority has passed before exposing a path.
                suffix = request.url.path[len(site_path) :]
                return Response(
                    status_code=308,
                    headers={
                        **HOSTED_HEADERS,
                        'Location': quote(canonical_site + suffix, safe='/@&'),
                    },
                )
        etag = '"' + blob.digest + '"'
        headers = {**HOSTED_HEADERS, 'ETag': etag, 'Accept-Ranges': 'bytes'}
        media = blob.media_type.split(';', 1)[0].strip().lower()
        if media in DOWNLOAD_TYPES:
            headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + quote(
                file_path.rsplit('/', 1)[-1], safe=''
            )
        if request.headers.get('if-none-match') == etag:
            return Response(status_code=304, headers=headers)
        byte_range, status, range_headers = hosted_range(request, blob.size, etag)
        headers.update(range_headers)
        headers['Content-Length'] = str(byte_range[1] - byte_range[0])
        if request.method == 'HEAD':
            return Response(status_code=status, media_type=blob.media_type, headers=headers)
        return StreamingResponse(
            service.contents.read(blob, byte_range),
            status_code=status,
            media_type=blob.media_type,
            headers=headers,
        )
    except Failure as exc:
        return hosted_error(exc.code)


def hosting_app(service):
    require(getattr(service, 'readonly_hosting', False), 'hosting_runtime_required')

    async def dispatch(request):
        try:
            service.require_ready()
            async with service.metadata.transaction(write=False) as tx:
                service.runtime_generation.require_current(tx)
        except Failure as exc:
            return hosted_error(exc.code)
        expected = urlsplit(service.settings.service_url)
        supplied = urlsplit('//' + request.headers.get('host', ''))
        if supplied.hostname != expected.hostname:
            return hosted_error('forbidden_host')
        return await serve_hosted(service, request) or hosted_error('not_found')

    return Starlette(
        routes=[Route('/{path:path}', dispatch, methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE'])]
    )
