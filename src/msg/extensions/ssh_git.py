"""SSH receive-pack entrypoint over the shared guarded Git publication path."""

from __future__ import annotations

from msg.core.errors import require

# Historical imports are the exact shared objects, never alternate wrappers.
from msg.extensions.git_publication import (
    ReferenceGuard as ReferenceGuard,
    guarded_command as guarded_command,
    hook_program as hook_program,
    relay_bounded_stdin as relay_bounded_stdin,
)
from msg.extensions.repositories import (
    MAX_GIT_PACK_BYTES,
    MAX_GIT_UPLOAD_SECONDS,
    NativeGitStore,
    require_git_repository_capacity,
)


async def receive_pack(app, job_id, *, stdin_stream=None):
    async with app.metadata.transaction(write=False) as tx:
        job = await tx.job(job_id)
        require(job.kind == 'git.receive' and job.state == 'running', 'invalid_git_session')
    require_git_repository_capacity(NativeGitStore(app).path(job.arguments['id']))
    return await guarded_command(
        app,
        job,
        lambda store: ['receive-pack', str(store.path(job.arguments['id']))],
        timeout=MAX_GIT_UPLOAD_SECONDS,
        stdin_byte_limit=MAX_GIT_PACK_BYTES,
        stdin_stream=stdin_stream,
    )
