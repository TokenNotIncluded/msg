"""Release-owned ASCII artwork for the shared board's untouched initial state."""

import re
from html import escape

DEFAULT_TEXT = '留一句话，让下一位接着写。'

# Explicit positions preserve the ASCII's spaces without CSS or xml:space.
# The SVG validator intentionally permits neither of those mechanisms.
_LETTERS = (
    ('__    __', '| \\  / |', '|  \\/  |', '| |\\/| |', '| |  | |', '|_|  |_|'),
    ('  _____ ', ' / ____|', '| (___  ', ' \\___ \\ ', ' ____) |', '|_____/ '),
    ('  _____ ', ' / ____|', '| |  __ ', '| | |_ |', '| |__| |', ' \\_____|'),
)


def default_art():
    """A legible ASCII wordmark; one quiet cursor is the only moving element."""
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 300">',
        '<title>MSG in ASCII</title>',
        '<desc>ASCII lettering with an animated cursor. Shared SVG and text.</desc>',
        '<g font-family="monospace" font-size="36" fill="#7d8491">',
    ]
    for letter, rows in enumerate(_LETTERS):
        for row, glyph in enumerate(rows):
            for run in re.finditer(r'\S+', glyph):
                x = 111 + (letter * 13 + run.start()) * 21.6
                parts.append(f'<text x="{x:g}" y="{69 + row * 33}">{escape(run.group())}</text>')
    parts.extend((
        '</g>',
        '<g font-family="monospace" font-size="17" fill="#7d8491">',
        '<text x="111" y="280">[</text>',
        '<text x="136" y="280">svg + text</text>',
        '<text x="254" y="280">]</text>',
        '<text x="829" y="280">&gt;</text>',
        '<text x="851" y="280" fill="#5d89bd">_',
        '<animate attributeName="fill" values="#5d89bd;#5d89bd;#7d8491;#5d89bd" '
        'keyTimes="0;0.75;0.875;1" dur="12s" repeatCount="indefinite"/>',
        '</text></g></svg>',
    ))
    return ''.join(parts)


DEFAULT_SVG = default_art()
