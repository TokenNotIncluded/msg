"""Line metadata for a unified diff, before pagination loses hunk context."""

import re

_HUNK = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def diff_rows(lines):
    """Keep difflib's line boundaries and assign both source line numbers.

    Do not split the joined diff: a source without a final newline makes
    adjacent removal/addition records indistinguishable after concatenation.
    """
    old = new = None
    for line in lines:
        match = _HUNK.match(line)
        if match:
            old, new = map(int, match.groups())
            kind, old_line, new_line = 'hunk', None, None
        elif old is None or new is None:
            kind, old_line, new_line = 'header', None, None
        elif line.startswith('-'):
            kind, old_line, new_line = 'delete', old, None
            old += 1
        elif line.startswith('+'):
            kind, old_line, new_line = 'insert', None, new
            new += 1
        elif line.startswith(' '):
            kind, old_line, new_line = 'context', old, new
            old += 1
            new += 1
        else:
            kind, old_line, new_line = 'note', None, None
        yield {'kind': kind, 'old_line': old_line, 'new_line': new_line, 'text': line}
