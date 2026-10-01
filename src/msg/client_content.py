"""Client-side immutable Revision signing for structured text patches.

The returned arguments go through the ordinary signed OperationRequest client.
A Revision signature is separate from both that request and the server receipt.
The caller must fetch and verify the exact current/base bytes beforehand.
"""

from uuid import uuid4

from msg.core.codec import canonical, digest, wire
from msg.core.errors import require
from msg.core.models import BlobRef, Revision
from msg.core.text_patch import apply_patch


def signed_patch_arguments(
    signer,
    resource,
    previous,
    source,
    patch,
    *,
    subject,
    created_at,
    actor=None,
    change_note=None,
    revision_id=None,
    base_revision=None,
    base_generation=None,
    base_source=None,
):
    require(
        previous.resource_id == resource.id and previous.id == resource.revision,
        'revision_conflict',
    )
    require(
        digest(source.encode('utf-8')) == previous.content.digest
        and len(source.encode('utf-8')) == previous.content.size,
        'content_digest_mismatch',
    )
    require(previous.content.media_type in {'text/plain', 'text/markdown'}, 'text_patch_required')
    require(base_revision is None or base_source is not None, 'base_content_required')
    body = apply_patch(source, patch, base_source=base_source)
    blob = BlobRef(
        digest=digest(body.encode('utf-8')),
        size=len(body.encode('utf-8')),
        media_type=previous.content.media_type,
    )
    revision = Revision(
        format_version=1,
        id=revision_id or 'v_' + uuid4().hex,
        resource_id=resource.id,
        parents=(previous.id,),
        content=blob,
        relations=previous.relations,
        actor=actor or subject,
        subject=subject,
        author=previous.author,
        created_at=created_at,
        manifest_digest='',
        summary=previous.summary,
        change_note=change_note,
        source_kind='user',
        source_version=1,
        source_digest=digest(patch),
    )
    manifest = {
        key: value
        for key, value in wire(revision).items()
        if key not in {'manifest_digest', 'signature'}
    }
    signature = signer.sign(canonical(manifest), purpose='revision')
    args = {
        'id': resource.id,
        'base_revision': base_revision or previous.id,
        'base_generation': resource.generation if base_generation is None else base_generation,
        'patch': patch,
        'revision_id': revision.id,
        'content_created_at': wire(created_at),
        'content_signature': wire(signature),
    }
    if change_note is not None:
        args['change_note'] = change_note
    if base_revision is not None:
        args['rebase'] = True
    return args
