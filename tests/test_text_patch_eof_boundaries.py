"""Unified patches cannot turn an EOF marker into an interior byte join."""

import subprocess

import pytest

from msg.core.errors import Failure
from msg.core.text_patch import apply_patch


@pytest.mark.parametrize('rebase', [False, True])
def test_new_eof_marker_rejects_remaining_source_lines_like_git(tmp_path, rebase):
    source = 'a\nuntouched\n'
    diff = '--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n'
    target = tmp_path / 'f'
    target.write_bytes(source.encode())
    result = subprocess.run(
        ['git', 'apply', '-'], cwd=tmp_path, input=diff.encode(), capture_output=True
    )
    assert result.returncode != 0
    assert target.read_bytes() == source.encode()
    kwargs = {'base_source': source} if rebase else {}
    with pytest.raises(Failure) as caught:
        apply_patch(source, {'kind': 'unified', 'diff': diff}, **kwargs)
    assert caught.value.code == 'invalid_unified_patch'


def test_unterminated_diff_payload_requires_explicit_eof_marker_like_git(tmp_path):
    source = 'a\n'
    diff = '--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b'
    target = tmp_path / 'f'
    target.write_bytes(source.encode())
    result = subprocess.run(
        ['git', 'apply', '-'], cwd=tmp_path, input=diff.encode(), capture_output=True
    )
    assert result.returncode != 0
    assert target.read_bytes() == source.encode()
    with pytest.raises(Failure) as caught:
        apply_patch(source, {'kind': 'unified', 'diff': diff})
    assert caught.value.code == 'invalid_unified_patch'


@pytest.mark.parametrize('rebase', [False, True])
def test_real_eof_marker_still_matches_git(tmp_path, rebase):
    source = 'prefix\na\n'
    diff = '--- a/f\n+++ b/f\n@@ -2 +2 @@\n-a\n+b\n\\ No newline at end of file\n'
    target = tmp_path / 'f'
    target.write_bytes(source.encode())
    subprocess.run(
        ['git', 'apply', '-'], cwd=tmp_path, input=diff.encode(), check=True, capture_output=True
    )
    kwargs = {'base_source': source} if rebase else {}
    assert (
        apply_patch(source, {'kind': 'unified', 'diff': diff}, **kwargs).encode()
        == target.read_bytes()
    )
    assert target.read_bytes() == b'prefix\nb'


def test_rebase_does_not_join_concurrently_appended_lines():
    base = 'a\n'
    current = base + 'concurrent\n'
    diff = '@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n'
    assert apply_patch(base, {'kind': 'unified', 'diff': diff}) == 'b'
    with pytest.raises(Failure) as caught:
        apply_patch(current, {'kind': 'unified', 'diff': diff}, base_source=base)
    assert caught.value.code == 'invalid_unified_patch'


@pytest.mark.asyncio
async def test_signed_patch_failure_does_not_publish_revision(installed):
    from test_service import call, register

    from msg.core.codec import wire

    app, _ = installed
    key, owner, _ = await register(app, 'patch-eof-owner')
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'a\nuntouched\n'},
        key=key,
        subject=owner,
    )
    assert post.status == 'ok', wire(post)
    rid = post.resources[0].id

    async def snapshot():
        async with app.metadata.transaction(write=False) as tx:
            resource = await tx.resource(rid)
            return (
                resource.generation,
                resource.revision,
                tx.one('SELECT COUNT(*) FROM revisions')[0],
                tx.one('SELECT COUNT(*) FROM events')[0],
            )

    before = await snapshot()
    failed = await call(
        app,
        'content.text_patch',
        {
            'id': rid,
            'base_revision': post.resources[0].revision,
            'base_generation': post.data['generation'],
            'patch': {
                'kind': 'unified',
                'diff': '@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n',
            },
        },
        key=key,
        subject=owner,
        expected=((rid, post.data['generation']),),
        contract_version=3,
    )
    assert failed.status == 'error' and failed.error.code == 'invalid_unified_patch', wire(failed)
    assert await snapshot() == before
