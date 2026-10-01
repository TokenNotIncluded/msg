"""Authenticated reading statements bound to immutable content and byte ranges."""

import hashlib

from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput, ResourceRef
from msg.core.requests import signing_bytes
from msg.plugins.common import check_access, resolve
from msg.plugins.schemas import IDENTIFIER, REF, STRING, obj

RANGE = obj(
    {
        'start': {'type': 'integer', 'minimum': 0},
        'end': {'type': 'integer', 'minimum': 1},
    },
    ('start', 'end'),
)
PART = obj({**RANGE['properties'], 'digest': STRING}, ('start', 'end', 'digest'))
LINES = obj(
    {'start': {'type': 'integer', 'minimum': 1}, 'end': {'type': 'integer', 'minimum': 1}},
    ('start', 'end'),
)


async def line_ranges(app, revision, selections):
    require(revision.content.media_type in {'text/plain', 'text/markdown'}, 'text_source_required')
    wanted = {n for part in selections for n in (part['start'], part['end'] + 1)}
    starts = {1: 0}
    line, offset, ends_newline = 1, 0, False
    async for chunk in app.contents.read(revision.content):
        position = 0
        while (position := chunk.find(b'\n', position)) != -1:
            line += 1
            position += 1
            if line in wanted:
                starts[line] = offset + position
        offset += len(chunk)
        if chunk:
            ends_newline = chunk.endswith(b'\n')
    count = line - int(ends_newline) if offset else 0
    result = []
    for part in selections:
        start, end = part['start'], part['end']
        require(1 <= start <= end <= count, 'reading_line_range_invalid')
        result.append({'start': starts[start], 'end': starts.get(end + 1, offset)})
    return result


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


async def describe(app, revision, ranges):
    parts = []
    for part in ranges:
        start, end = part['start'], part['end']
        require(0 <= start < end <= revision.content.size, 'reading_range_invalid')
        hasher = hashlib.sha256()
        async for chunk in app.contents.read(revision.content, (start, end)):
            hasher.update(chunk)
        parts.append({'start': start, 'end': end, 'digest': 'sha256:' + hasher.hexdigest()})
    return parts


def install(app, op):
    async def target_revision(ctx, request, tx, ref):
        require(ref.revision is not None, 'reading_revision_required')
        resource = await tx.resource(ref.id)
        require(resource.type == 'post' and resource.state == 'active', 'not_a_post')
        await check_access(app, ctx, request, tx, ref.id, 'read')
        return await tx.revision(ref)

    @op(
        'discussion.reading_manifest',
        obj(
            {
                'target': REF,
                'ranges': {'type': 'array', 'items': RANGE, 'minItems': 1, 'maxItems': 128},
                'line_ranges': {'type': 'array', 'items': LINES, 'minItems': 1, 'maxItems': 128},
            },
            ('target',),
        ),
        effect='read',
    )
    async def manifest(ctx, request, tx):
        ref = decode(ResourceRef, request.arguments['target'])
        rev = await target_revision(ctx, request, tx, ref)
        ranges = request.arguments.get('ranges')
        if 'line_ranges' in request.arguments:
            require(ranges is None, 'reading_selection_conflict')
            ranges = await line_ranges(app, rev, request.arguments['line_ranges'])
        if ranges is None:
            ranges = [{'start': 0, 'end': rev.content.size}] if rev.content.size else []
        return HandlerOutput(
            data={
                'format': 'msg.reading/1',
                'target': wire(ref),
                'digest': rev.content.digest,
                'manifest_digest': rev.manifest_digest,
                'byte_length': rev.content.size,
                'media_type': rev.content.media_type,
                'parts': await describe(app, rev, ranges),
            }
        )

    @op(
        'discussion.reading_prove',
        obj(
            {
                'target': REF,
                'digest': STRING,
                'parts': {'type': 'array', 'items': PART, 'minItems': 1, 'maxItems': 128},
                'note': {'type': 'string', 'maxLength': 4096},
            },
            ('target', 'digest', 'parts'),
        ),
    )
    async def prove(ctx, request, tx):
        from msg.plugins.discussion import gates
        from msg.storage.capacity import require_reaction_capacity

        require(ctx.principal.subject is not None, 'authentication_required')
        require(ctx.principal.method in {'signature', 'token'}, 'proof_signature_or_token_required')
        ref = decode(ResourceRef, request.arguments['target'])
        rev = await target_revision(ctx, request, tx, ref)
        await gates(app, ctx, request, tx, ref.id)
        require(request.arguments['digest'] == rev.content.digest, 'proof_digest_mismatch')
        parts = wire(request.arguments['parts'])
        require(parts == await describe(app, rev, parts), 'reading_part_digest_mismatch')
        coverage = merge_ranges((p['start'], p['end']) for p in parts)
        record = {
            'format': 'msg.reading/1',
            'target': wire(ref),
            'digest': rev.content.digest,
            'manifest_digest': rev.manifest_digest,
            'byte_length': rev.content.size,
            'parts': parts,
            'coverage': coverage,
            'covered_bytes': sum(end - start for start, end in coverage),
            'complete': coverage == [[0, rev.content.size]],
            'actor': ctx.principal.actor,
            'subject': ctx.principal.subject,
            'auth': ctx.principal.method,
            'time': wire(ctx.now),
            'note': request.arguments.get('note', ''),
        }
        # Distinct selections/evidence coexist; retries preserve the first signed statement.
        claim = digest({'parts': parts, 'note': record['note']})
        stored = 'reading.' + claim + '.' + ctx.principal.method
        existing = tx.one(
            'SELECT body FROM reactions WHERE subject=? AND resource=? AND kind=? AND revision=?',
            (ctx.principal.subject, ref.id, stored, rev.id),
        )
        if existing:
            return HandlerOutput(resources=(ref,), data=loads(existing[0]))
        require_reaction_capacity(tx)
        if ctx.principal.method == 'signature':
            record.update(
                signature=wire(request.proof.signature), signed_envelope=b64(signing_bytes(request))
            )
        tx.execute(
            'INSERT INTO reactions VALUES (?,?,?,?,?) '
            'ON CONFLICT(subject,resource,kind,revision) DO NOTHING',
            (ctx.principal.subject, ref.id, stored, rev.id, canonical(record).decode()),
            write=True,
        )
        return HandlerOutput(resources=(ref,), data=record)

    @op(
        'discussion.readings',
        obj(
            {
                'id': IDENTIFIER,
                'revision': IDENTIFIER,
                'cursor': STRING,
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
            },
            ('id',),
        ),
        effect='read',
    )
    async def readings(ctx, request, tx):
        rid = await resolve(tx, request.arguments['id'])
        resource = await tx.resource(rid)
        ref = ResourceRef(id=rid, revision=request.arguments.get('revision', resource.revision))
        rev = await target_revision(ctx, request, tx, ref)
        binding = digest({'id': rid, 'revision': rev.id})
        position = (
            app.cursors.decode(request.arguments['cursor'], 'readings', binding)
            if request.arguments.get('cursor')
            else ['', '']
        )
        limit = request.arguments.get('limit', 50)
        rows = tx.rows(
            'SELECT body,subject,kind FROM reactions WHERE resource=? AND revision=? '
            "AND kind LIKE 'reading.%' AND (subject,kind)>(?,?) ORDER BY subject,kind LIMIT ?",
            (rid, rev.id, *position, limit + 1),
        )
        mine = tx.rows(
            'SELECT body FROM reactions WHERE resource=? AND revision=? AND subject=? '
            "AND kind LIKE 'reading.%'",
            (rid, rev.id, ctx.principal.subject),
        )
        coverage = merge_ranges(pair for row in mine for pair in loads(row[0])['coverage'])
        data = {
            'target': wire(ref),
            'digest': rev.content.digest,
            'byte_length': rev.content.size,
            'items': [loads(row[0]) for row in rows[:limit]],
            'my_coverage': coverage,
            'my_covered_bytes': sum(end - start for start, end in coverage),
            'my_complete': coverage == [[0, rev.content.size]],
        }
        if len(rows) > limit:
            data['cursor'] = app.cursors.encode('readings', binding, list(rows[limit - 1][1:]))
        return HandlerOutput(data=data)
