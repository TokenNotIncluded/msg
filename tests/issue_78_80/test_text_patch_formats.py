"""Byte-preserving patch vectors; no database or network service required."""

import difflib

import pytest

from msg.core.codec import digest
from msg.core.errors import Failure
from msg.core.text_patch import apply_patch, validate_patch


def fails(code, source, patch, **kwargs):
    with pytest.raises(Failure) as exc:
        apply_patch(source, patch, **kwargs)
    assert exc.value.code == code


def udiff(old, new):
    return ''.join(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), fromfile='a/file', tofile='b/file'
        )
    )


@pytest.mark.parametrize(
    'old,new',
    [
        ('a\nb\nc\n', 'a\nB\nc\n'),
        ('one\r\ntwo\r\n', 'one\r\nTWO\r\n'),
        ('中文\n原文\n', '中文\n修订\n'),
        ('a\n', 'a\nb\n'),
        ('a\n', ''),
        ('', 'b\n'),
        ('a\nb\nc\nd\ne\nf\ng\nh\ni\n', 'A\nb\nc\nd\ne\nf\ng\nh\nI\n'),
    ],
)
def test_unified_round_trip(old, new):
    assert apply_patch(old, {'kind': 'unified', 'diff': udiff(old, new)}) == new


def test_no_newline_marker_preserves_exact_bytes():
    diff = '--- a/f\n+++ b/f\n@@ -1 +1 @@\n-before\n\\ No newline at end of file\n+after\n\\ No newline at end of file\n'
    assert apply_patch('before', {'kind': 'unified', 'diff': diff}) == 'after'


@pytest.mark.parametrize(
    'diff',
    [
        '@@ -1,2 +1 @@\n-a\n+b\n',
        '@@ -1 +2 @@\n-a\n+b\n',
        '@@ -0 +1 @@\n-a\n+b\n',
        '--- a/f\n+++ b/f\n',
        '@@ -1 +1 @@\n-a\n+b\ngarbage\n',
        '@@ -1 +1 @@\n-a\n+b\n--- a/g\n+++ b/g\n@@ -1 +1 @@\n-x\n+y\n',
        '@@ -1 +1 @@\n\\ No newline at end of file\n-a\n+b\n',
        '@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n\\ No newline at end of file\n',
    ],
)
def test_malformed_or_multiple_file_diff_rejected(diff):
    fails('invalid_unified_patch', 'a\n', {'kind': 'unified', 'diff': diff})


def test_unified_requires_base_bytes():
    fails('patch_no_match', 'not a\n', {'kind': 'unified', 'diff': '@@ -1 +1 @@\n-a\n+b\n'})


def test_rebase_only_unchanged_unique_hunk():
    old = 'a\nb\nc\n'
    patch = {'kind': 'unified', 'diff': udiff(old, 'a\nB\nc\n')}
    assert apply_patch('prefix\n' + old, patch, base_source=old) == 'prefix\na\nB\nc\n'
    fails('patch_no_match', 'a\nconcurrent\nc\n', patch, base_source=old)
    fails('patch_ambiguous', old + old, patch, base_source=old)


def test_rebase_unanchored_insertion_is_not_guessed():
    fails(
        'patch_rebase_unsupported',
        'concurrent\n',
        {'kind': 'unified', 'diff': '@@ -0,0 +1 @@\n+new\n'},
        base_source='',
    )


def test_heading_patch_ignores_fences_and_keeps_other_sections():
    target = '## Target\noriginal\n### Child\nchild\n'
    source = '# Document\n```md\n## Target\n```\n' + target + '## Other\nkeep\n'
    patch = {
        'kind': 'heading',
        'heading': 'Target',
        'digest': digest(target.encode()),
        'replacement': '## Target\nreplacement\n',
    }
    assert apply_patch(source, patch) == source.replace(target, patch['replacement'])


def test_heading_rebase_requires_unchanged_section():
    base = '# A\nx\n# B\ny\n'
    patch = {
        'kind': 'heading',
        'heading': 'A',
        'digest': digest(b'# A\nx\n'),
        'replacement': '# A\nnew\n',
    }
    assert (
        apply_patch(base.replace('y\n', 'else\n'), patch, base_source=base)
        == '# A\nnew\n# B\nelse\n'
    )
    fails('patch_no_match', base.replace('x\n', 'changed\n'), patch, base_source=base)


@pytest.mark.parametrize(
    'source,selected',
    [
        ('Title\n=====\nbody\nNext\n----\nchild\n', 'Title\n=====\nbody\nNext\n----\nchild\n'),
        ('## Title ##\nbody\n## End\nend\n', '## Title ##\nbody\n'),
        ('## Title\r\nbody\r\n## End\r\nend\r\n', '## Title\r\nbody\r\n'),
    ],
)
def test_heading_variants(source, selected):
    assert apply_patch(
        source,
        {
            'kind': 'heading',
            'heading': 'Title',
            'digest': digest(selected.encode()),
            'replacement': 'NEW\n',
        },
    ) == source.replace(selected, 'NEW\n')


def test_duplicate_heading_rejected_even_with_digest():
    fails(
        'patch_ambiguous',
        '# X\na\n# X\nb\n',
        {'kind': 'heading', 'heading': 'X', 'digest': digest(b'# X\na\n'), 'replacement': 'new'},
    )


def test_block_digest_and_fenced_blank_lines():
    block = '```\none\n\ntwo\n```\n'
    source = 'paragraph\n\n' + block + '\nlast\n'
    assert (
        apply_patch(
            source, {'kind': 'block', 'digest': digest(block.encode()), 'replacement': 'new\n'}
        )
        == 'paragraph\n\nnew\n\nlast\n'
    )
    fails(
        'patch_no_match',
        source,
        {'kind': 'block', 'digest': digest(b'one\n'), 'replacement': 'bad'},
    )


def test_duplicate_block_rejected():
    fails(
        'patch_ambiguous',
        'same\n\nsame\n',
        {'kind': 'block', 'digest': digest(b'same\n'), 'replacement': 'new\n'},
    )


@pytest.mark.parametrize(
    'patch',
    [
        {'kind': 'block', 'digest': 'aabb', 'replacement': 'x'},
        {
            'kind': 'heading',
            'heading': 'X',
            'digest': digest(b'x'),
            'replacement': 'x',
            'extra': True,
        },
        {'kind': 'exact', 'exact': '', 'replacement': 'x'},
        {'kind': 'unified', 'diff': '', 'replacement': 'x'},
        {'kind': 'unknown'},
    ],
)
def test_closed_patch_schema(patch):
    with pytest.raises(Failure):
        validate_patch(patch)


def test_exact_compatibility_and_context():
    patch = {'kind': 'exact', 'exact': 'x', 'replacement': 'y', 'before': 'b'}
    assert apply_patch('ax bx', patch) == 'ax by'
    fails('patch_ambiguous', 'x x', {'kind': 'exact', 'exact': 'x', 'replacement': 'y'})


def test_byte_not_character_size_limit():
    fails(
        'patch_too_large', 'é' * (524288 + 1), {'kind': 'exact', 'exact': 'é', 'replacement': 'x'}
    )


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_unified_diff_seeded_roundtrips(newline):
    import difflib
    import random

    rng = random.Random(7880)
    for _ in range(150):
        old = [
            rng.choice(['alpha', '重复', '', '  code']) + newline
            for i in range(rng.randrange(1, 40))
        ]
        new = old.copy()
        start = rng.randrange(len(new) + 1)
        end = rng.randrange(start, len(new) + 1)
        new[start:end] = [
            rng.choice(['changed', '中文', '']) + newline for i in range(rng.randrange(1, 8))
        ]
        diff = ''.join(difflib.unified_diff(old, new, fromfile='before', tofile='after'))
        if diff:
            assert apply_patch(''.join(old), {'kind': 'unified', 'diff': diff}) == ''.join(new)
