"""Revision-bound authenticated claims, never a popularity score."""

from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput, ResourceRef
from msg.core.requests import signing_bytes
from msg.plugins.common import check_access, resolve
from msg.plugins.schemas import IDENTIFIER, REF, STRING, obj

PROOF_KINDS = ('ACK', 'USED', 'VERIFIED', 'SOLVED', 'THANKS')


def proof_kind(value):
    return 'VERIFIED' if value == 'VERIFED' else value


async def projection(tx, rid, revision, subject):
    rows = tx.rows(
        'SELECT subject,kind FROM reactions WHERE resource=? AND revision=? '
        "AND (kind LIKE 'proof.%' OR kind IN ('ack.signature','ack.token'))",
        (rid, revision),
    )
    counts = {kind: set() for kind in PROOF_KINDS}
    mine = set()
    for actor, stored in rows:
        kind = 'ACK' if stored.startswith('ack.') else stored.split('.')[1]
        if kind in counts:
            counts[kind].add(actor)
            if actor == subject:
                mine.add(kind)
    return {
        'revision': revision,
        'proofs': {k: len(v) for k, v in counts.items()},
        'my_proofs': sorted(mine),
    }


def install(app, op):
    from msg.plugins.reading_proofs import install as install_reading

    install_reading(app, op)

    @op(
        'discussion.prove',
        obj(
            {
                'target': REF,
                'digest': STRING,
                'kind': {'enum': [*PROOF_KINDS, 'VERIFED']},
                'note': {'type': 'string', 'maxLength': 4096},
            },
            ('target', 'digest', 'kind'),
        ),
    )
    async def prove(ctx, request, tx):
        from msg.plugins.discussion import gates
        from msg.storage.capacity import require_reaction_capacity

        require(ctx.principal.subject is not None, 'authentication_required')
        require(ctx.principal.method in {'signature', 'token'}, 'proof_signature_or_token_required')
        target = decode(ResourceRef, request.arguments['target'])
        require(target.revision is not None, 'proof_revision_required')
        resource = await tx.resource(target.id)
        require(resource.type == 'post' and resource.state == 'active', 'not_a_post')
        await check_access(app, ctx, request, tx, target.id, 'read')
        await gates(app, ctx, request, tx, target.id)
        rev = await tx.revision(target)
        require(
            request.arguments['digest'] in {rev.content.digest, rev.manifest_digest},
            'proof_digest_mismatch',
        )
        kind = proof_kind(request.arguments['kind'])
        method = ctx.principal.method
        stored = ('ack.' + method) if kind == 'ACK' else ('proof.' + kind + '.' + method)
        record = {
            'kind': kind,
            'actor': ctx.principal.actor,
            'subject': ctx.principal.subject,
            'auth': method,
            'revision': rev.id,
            'digest': request.arguments['digest'],
            'time': wire(ctx.now),
            'note': request.arguments.get('note', ''),
        }
        if method == 'signature':
            record.update(
                signature=wire(request.proof.signature), signed_envelope=b64(signing_bytes(request))
            )
        if not tx.one(
            'SELECT 1 FROM reactions WHERE subject=? AND resource=? AND kind=? AND revision=?',
            (ctx.principal.subject, target.id, stored, rev.id),
        ):
            require_reaction_capacity(tx)
        tx.execute(
            'INSERT INTO reactions VALUES (?,?,?,?,?) ON CONFLICT(subject,resource,kind,revision) DO NOTHING',
            (ctx.principal.subject, target.id, stored, rev.id, canonical(record).decode()),
            write=True,
        )
        return HandlerOutput(
            resources=(target,), data=await projection(tx, target.id, rev.id, ctx.principal.subject)
        )

    @op(
        'discussion.proofs',
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
    async def proofs(ctx, request, tx):
        rid = await resolve(tx, request.arguments['id'])
        resource = await tx.resource(rid)
        require(resource.type == 'post' and resource.state == 'active', 'not_a_post')
        await check_access(app, ctx, request, tx, rid, 'read')
        rev = await tx.revision(ResourceRef(id=rid, revision=request.arguments.get('revision')))
        binding = digest({'id': rid, 'revision': rev.id})
        position = (
            app.cursors.decode(request.arguments['cursor'], 'proofs', binding)
            if request.arguments.get('cursor')
            else ['', '']
        )
        limit = request.arguments.get('limit', 50)
        rows = tx.rows(
            'SELECT body,subject,kind FROM reactions WHERE resource=? AND revision=? '
            "AND (kind LIKE 'proof.%' OR kind IN ('ack.signature','ack.token')) "
            'AND (subject,kind)>(?,?) ORDER BY subject,kind LIMIT ?',
            (rid, rev.id, *position, limit + 1),
        )
        data = await projection(tx, rid, rev.id, ctx.principal.subject)
        data['items'] = [{'kind': 'ACK', **loads(row[0])} for row in rows[:limit]]
        if len(rows) > limit:
            data['cursor'] = app.cursors.encode('proofs', binding, list(rows[limit - 1][1:]))
        return HandlerOutput(data=data)
