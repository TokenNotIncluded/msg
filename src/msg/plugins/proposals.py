"""Version one proposals replace a text post with an immutable content revision."""

from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, Relation, ResourceRef
from msg.plugins.collaboration import _safe_ref, _self
from msg.plugins.collaboration_resources import _folder, _load, _result
from msg.plugins.common import (
    assert_generation,
    check_access,
    create_resource,
    new_id,
    revise_resource,
)
from msg.plugins.schemas import IDENTIFIER, obj

PINNED_REF = obj({'id': IDENTIFIER, 'revision': IDENTIFIER}, ('id', 'revision'))


def install(app, op):
    @op(
        'communication.proposal_create',
        obj(
            {
                'target': IDENTIFIER,
                'base_revision': IDENTIFIER,
                'content_ref': PINNED_REF,
                'message': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
            },
            ('target', 'base_revision', 'content_ref', 'message'),
        ),
        signature=True,
    )
    async def create(ctx, request, tx):
        subject = await _self(app, ctx, request, tx)
        args = request.arguments
        target_id = await _safe_ref(app, ctx, request, tx, args['target'])
        target = await tx.resource(target_id)
        require(target.type == 'post', 'proposal_target_kind_unsupported')
        require(target.revision == args['base_revision'], 'revision_conflict')
        target_ref = ResourceRef(id=target.id, revision=args['base_revision'])
        await tx.revision(target_ref)
        source_id = await _safe_ref(app, ctx, request, tx, args['content_ref']['id'])
        source_ref = ResourceRef(id=source_id, revision=args['content_ref']['revision'])
        source = await tx.revision(source_ref)
        require(source.content.media_type.startswith('text/'), 'proposal_content_kind_unsupported')
        require(
            source.content.size <= app.settings.server.limits.max_response_bytes,
            'proposal_content_too_large',
        )
        # Verify textual content now. Acceptance reuses the pinned BlobRef, never
        # the source's mutable latest revision or client-provided replacement bytes.
        try:
            (await app.contents.read_bytes(source.content)).decode('utf-8')
        except UnicodeDecodeError as exc:
            raise Failure('proposal_content_encoding_invalid') from exc
        parent = await _folder(app, ctx, request, tx, subject, 'proposal')
        record = {
            'subject': subject,
            'author': subject,
            'message': args['message'],
            'created_at': wire(ctx.now),
            'status': 'open',
        }
        resource = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent=parent,
            type='collab_proposal',
            name=new_id('proposal'),
            body=canonical(record),
            media_type='application/json',
            mode=0o600,
            relations=(
                Relation(type='target', target=target_ref),
                Relation(type='content', target=source_ref),
            ),
        )
        return _result(resource, record)

    decision_schema = obj(
        {'id': IDENTIFIER, 'proposal_revision': IDENTIFIER}, ('id', 'proposal_revision')
    )
    accept_schema = obj(
        {'id': IDENTIFIER, 'proposal_revision': IDENTIFIER, 'base_revision': IDENTIFIER},
        ('id', 'proposal_revision', 'base_revision'),
    )

    def install_decision(action):
        @op(
            'communication.proposal_' + action,
            accept_schema if action == 'accept' else decision_schema,
            signature=True,
        )
        async def decide(ctx, request, tx):
            subject = await _self(app, ctx, request, tx)
            proposal, revision, record = await _load(app, ctx, request, tx, 'proposal')
            await assert_generation(request, proposal)
            require(revision.id == request.arguments['proposal_revision'], 'revision_conflict')
            require(record['status'] == 'open', 'proposal_not_open')
            refs = {r.type: r.target for r in revision.relations}
            require(set(refs) == {'target', 'content'}, 'proposal_record_invalid')
            changed = None
            if action == 'withdraw':
                require(subject == record['author'], 'collaboration_actor_forbidden')
            else:
                target_id = await _safe_ref(app, ctx, request, tx, refs['target'].id)
                await check_access(app, ctx, request, tx, target_id, 'write')
                if action == 'accept':
                    from msg.plugins.content import editable_resource

                    target = await editable_resource(app, ctx, request, tx, target_id)
                    require(target.type == 'post', 'proposal_target_kind_unsupported')
                    require(
                        request.arguments['base_revision']
                        == refs['target'].revision
                        == target.revision,
                        'revision_conflict',
                    )
                    await _safe_ref(app, ctx, request, tx, refs['content'].id)
                    source = await tx.revision(refs['content'])
                    require(
                        source.content.media_type.startswith('text/'),
                        'proposal_content_kind_unsupported',
                    )
                    previous = await tx.revision(ResourceRef(id=target.id))
                    changed = await revise_resource(
                        app,
                        ctx,
                        request,
                        tx,
                        target,
                        source.content,
                        source.content.media_type,
                        relations=previous.relations,
                        author=previous.author,
                    )
            record.update(
                status={'accept': 'accepted', 'reject': 'rejected', 'withdraw': 'withdrawn'}[
                    action
                ],
                updated_at=wire(ctx.now),
            )
            # Decisions can withdraw/reject after target access is lost. The old
            # immutable revision still proves the proposal's originally pinned refs.
            relations = revision.relations if action == 'accept' else ()
            proposal = await revise_resource(
                app,
                ctx,
                request,
                tx,
                proposal,
                canonical(record),
                'application/json',
                relations=relations,
            )
            output = _result(proposal, record)
            if changed is None:
                return output
            return HandlerOutput(
                resources=(
                    *output.resources,
                    ResourceRef(id=changed.id, revision=changed.revision),
                ),
                data=output.data,
            )

    for action in ('accept', 'reject', 'withdraw'):
        install_decision(action)
