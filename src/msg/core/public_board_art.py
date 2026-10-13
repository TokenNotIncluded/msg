"""Release-owned ASCII artwork for the shared board's untouched initial state."""

from html import escape

DEFAULT_TEXT = '写下一句，让它继续走。'

# A 7x9 signal-board face. Lit cells carry the bits of MESSAGE; dark cells
# stay as a faint dot, so every glyph is ASCII and no CSS is needed.
_FACE = (
    (
        '#.....#',
        '##...##',
        '#.#.#.#',
        '#..#..#',
        '#.....#',
        '#.....#',
        '#.....#',
        '#.....#',
        '#.....#',
    ),
    (
        '.#####.',
        '#.....#',
        '#......',
        '.##....',
        '...###.',
        '......#',
        '......#',
        '#.....#',
        '.#####.',
    ),
    (
        '.#####.',
        '#.....#',
        '#......',
        '#......',
        '#...###',
        '#.....#',
        '#.....#',
        '#....##',
        '.####.#',
    ),
)
MESSAGE = 'msg: pass it on'
_INK = '#7b8798'
_DOT = '#8a94a4'
_SIGNAL = '#4f86c6'
_COLUMNS = (250, 480, 710)
_CELL = 22
_FONT = 20
_TOP = 44
_WIRE_Y = 268


def _animation(tag, attribute, values, times, **attributes):
    """A shared clock and matching endpoints keep the loop and still coherent."""
    extras = ''.join(f' {name}="{value}"' for name, value in attributes.items())
    return (
        f'<{tag} attributeName="{attribute}" values="{values}" '
        f'keyTimes="{times}" dur="12s" repeatCount="indefinite"{extras}/>'
    )


def _travel(values, times, kind='translate'):
    return _animation(
        'animateTransform',
        'transform',
        values,
        times,
        type=kind,
        calcMode='spline',
        keySplines=';'.join(('0.16 0 0.3 1',) * (len(times.split(';')) - 1)),
    )


def _text(x, y, value, **attributes):
    extras = ''.join(f' {name.replace("_", "-")}="{attr}"' for name, attr in attributes.items())
    return f'<text x="{x:g}" y="{y:g}"{extras}>{escape(value)}</text>'


def _bits():
    while True:
        yield from ''.join(f'{byte:08b}' for byte in MESSAGE.encode())


def default_art():
    """A packet crosses the wire; each letter it reaches lights and lifts once."""
    bits = _bits()
    half = (len(_FACE[0][0]) - 1) / 2 * _CELL
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 300">',
        '<title>MSG signal board</title>',
        '<desc>svg + text</desc>',
        f'<g font-family="monospace" font-size="{_FONT}" fill="{_INK}" text-anchor="middle">',
    ]
    for letter, (face, column) in enumerate(zip(_FACE, _COLUMNS, strict=True)):
        arrive = 0.3 + letter * 0.14
        parts.append('<g>')
        parts.append(
            _animation(
                'animate',
                'fill',
                f'{_INK};{_INK};{_SIGNAL};{_INK};{_INK}',
                f'0;{arrive - 0.03:g};{arrive:g};{arrive + 0.16:g};1',
            )
        )
        parts.append(
            _travel(
                '0 0;0 0;0 -8;0 0;0 0',
                f'0;{arrive - 0.03:g};{arrive + 0.02:g};{arrive + 0.12:g};1',
            )
        )
        for row, pattern in enumerate(face):
            y = _TOP + row * _CELL
            for column_index, cell in enumerate(pattern):
                x = column - half + column_index * _CELL
                if cell == '#':
                    parts.append(_text(x, y, next(bits)))
                else:
                    parts.append(_text(x, y, '.', fill=_DOT, fill_opacity='0.38'))
        parts.append('</g>')
    parts.append('</g>')
    parts.extend((
        f'<line x1="96" y1="{_WIRE_Y}" x2="864" y2="{_WIRE_Y}" stroke="{_DOT}" '
        'stroke-opacity="0.45" stroke-width="1.5" stroke-linecap="round"/>',
    ))
    for letter, column in enumerate(_COLUMNS):
        arrive = 0.3 + letter * 0.14
        parts.extend((
            f'<circle cx="{column}" cy="{_WIRE_Y}" r="5" fill="{_DOT}" fill-opacity="0.7">',
            _animation(
                'animate',
                'fill',
                f'{_DOT};{_DOT};{_SIGNAL};{_SIGNAL};{_DOT};{_DOT}',
                f'0;{arrive - 0.02:g};{arrive:g};0.82;0.9;1',
            ),
            '</circle>',
        ))
    parts.extend((
        f'<g fill="{_SIGNAL}" opacity="0">',
        _travel('96 0;96 0;864 0;864 0;96 0', '0;0.2;0.68;0.74;1'),
        _animation('animate', 'opacity', '0;0;1;1;0;0', '0;0.2;0.23;0.66;0.7;1'),
        f'<circle cx="0" cy="{_WIRE_Y}" r="10" fill-opacity="0.22"/>',
        f'<circle cx="0" cy="{_WIRE_Y}" r="4"/>',
        '</g>',
        '<g font-family="monospace" font-size="16" fill="#7d8491">',
        _text(96, 246, '>'),
        f'<text x="112" y="246" fill="{_SIGNAL}" fill-opacity="1">_',
        _animation('animate', 'fill-opacity', '1;1;0;1;0;1;1', '0;0.72;0.76;0.8;0.84;0.88;1'),
        '</text>',
        _text(864, 246, 'pass it on', text_anchor='end'),
        '</g></svg>',
    ))
    return ''.join(parts)


DEFAULT_SVG = default_art()
