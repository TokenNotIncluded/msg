"""Git diff lines are LF-delimited; Unicode separators are signed content bytes."""

import subprocess

import pytest

from msg.core.codec import digest
from msg.core.errors import Failure
from msg.core.text_patch import apply_patch

UNICODE_SEPARATORS = ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')


@pytest.mark.parametrize('separator', (*UNICODE_SEPARATORS, '\r'))
def test_unified_patch_preserves_non_lf_bytes_and_agrees_with_git(tmp_path, separator):
    before = f'left{separator}old\nunchanged\n'
    after = f'left{separator}new\nunchanged\n'
    diff = (
        '--- a/document.txt\n+++ b/document.txt\n@@ -1,2 +1,2 @@\n'
        f'-left{separator}old\n+left{separator}new\n unchanged\n'
    )
    target = tmp_path / 'document.txt'
    target.write_bytes(before.encode())
    subprocess.run(
        ['git', 'apply', '-'], cwd=tmp_path, input=diff.encode(), check=True, capture_output=True
    )
    assert target.read_bytes() == after.encode()
    assert apply_patch(before, {'kind': 'unified', 'diff': diff}).encode() == target.read_bytes()
    rebased = apply_patch(
        'prefix\n' + before, {'kind': 'unified', 'diff': diff}, base_source=before
    )
    assert rebased.encode() == ('prefix\n' + after).encode()


@pytest.mark.parametrize('separator', UNICODE_SEPARATORS)
def test_inline_unicode_separator_cannot_forge_a_markdown_heading(separator):
    source = f'inline{separator}# Not a heading\nparagraph\n'
    fake_section = '# Not a heading\nparagraph\n'
    with pytest.raises(Failure) as caught:
        apply_patch(
            source,
            {
                'kind': 'heading',
                'heading': 'Not a heading',
                'digest': digest(fake_section.encode()),
                'replacement': 'tampered\n',
            },
        )
    assert caught.value.code == 'patch_no_match'
    assert (
        apply_patch(
            source,
            {'kind': 'block', 'digest': digest(source.encode()), 'replacement': 'replaced\n'},
        )
        == 'replaced\n'
    )


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_markdown_keeps_real_line_endings_and_signed_section_digest(ending):
    section = '# Title' + ending + 'text' + ending
    source = section + '# Other' + ending + 'untouched' + ending
    patch = {
        'kind': 'heading',
        'heading': 'Title',
        'digest': digest(section.encode()),
        'replacement': '# New' + ending,
    }
    assert (
        apply_patch(source, patch) == '# New' + ending + '# Other' + ending + 'untouched' + ending
    )


def test_unified_no_newline_markers_preserve_inline_unicode():
    before, after = 'same\u2028old', 'same\u2028new'
    patch = {
        'kind': 'unified',
        'diff': '@@ -1 +1 @@\n-same\u2028old\n'
        '\\ No newline at end of file\n+same\u2028new\n\\ No newline at end of file\n',
    }
    assert apply_patch(before, patch) == after
