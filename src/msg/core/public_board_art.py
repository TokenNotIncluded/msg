"""Release-owned ASCII artwork for the shared board's untouched initial state."""

import re
from html import escape

DEFAULT_TEXT = '留一句话，让下一位接着写。'

# Explicit positions preserve ASCII spaces without CSS or xml:space.
_LETTERS = (
    ('__    __', '| \\  / |', '|  \\/  |', '| |\\/| |', '| |  | |', '|_|  |_|'),
    ('  _____ ', ' / ____|', '| (___  ', ' \\___ \\ ', ' ____) |', '|_____/ '),
    ('  _____ ', ' / ____|', '| |  __ ', '| | |_ |', '| |__| |', ' \\_____|'),
)
_INK = '#8491a3'
_SIGNAL = '#afc9e7'


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


def _packet(values, times, opacity_times, glyph):
    return ''.join((
        f'<g font-family="monospace" font-size="23" fill="{_SIGNAL}" opacity="0">',
        _travel(values, times),
        _animation('animate', 'opacity', '0;0;1;1;0;0', opacity_times),
        _text(-22, 6, ':', fill_opacity='0.3'),
        _text(-8, 6, glyph),
        '</g>',
    ))


def default_art():
    """The large word gathers, unfolds into a two-route relay, then returns."""
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 300">',
        '<title>MSG in ASCII</title>',
        '<desc>ASCII character streams gather into a large MSG. The word folds '
        'into a communication hub, with two packets traveling between bracketed '
        'message nodes. The twelve-second loop returns to the complete word. '
        'Shared SVG and text.</desc>',
        '<g transform="translate(480 135)">',
        _travel('480 135;480 135;480 69;480 69;480 135;480 135', '0;0.34;0.43;0.77;0.89;1'),
        f'<g font-family="monospace" font-size="39" fill="{_INK}">',
        _travel('1;1;0.46;0.46;1;1', '0;0.34;0.43;0.77;0.89;1', kind='scale'),
    ]
    for letter, rows in enumerate(_LETTERS):
        parts.append('<g>')
        arrival = 0.2 + letter * 0.035
        parts.append(
            _animation(
                'animate',
                'fill',
                f'{_INK};{_INK};{_SIGNAL};{_INK};{_INK}',
                f'0;{arrival - 0.04:g};{arrival:g};{arrival + 0.09:g};1',
            )
        )
        for row, glyph in enumerate(rows):
            sign = 1 if row % 2 == 0 else -1
            # Alternating streams spread across the whole stage, then arrive
            # in order. The full word is the base state, never a hidden loader.
            dx = sign * (38 + row * 4) + (letter - 1) * 13
            dy = (row - 2.5) * 10
            parts.extend((
                '<g>',
                _travel(
                    f'0 0;0 0;{dx} {dy:g};{-dx * 0.18:g} {-dy * 0.18:g};0 0;0 0',
                    f'0;0.035;0.105;{0.145 + row * 0.004:g};{arrival + row * 0.006:g};1',
                ),
            ))
            for run in re.finditer(r'\S+', glyph):
                x = -384 + (letter * 13 + run.start()) * 23.4
                parts.append(_text(x, -79 + row * 35, run.group()))
            parts.append('</g>')
        parts.append('</g>')
    parts.extend((
        '</g></g>',
        # The alternative composition is entirely ASCII. It only appears after
        # the large word has folded away, so the two scenes never fight.
        '<g font-family="monospace" font-size="23" fill="#7d8491" opacity="0">',
        _animation('animate', 'opacity', '0;0;1;1;0;0', '0;0.37;0.44;0.76;0.84;1'),
        _text(100, 180, '[ ]'),
        _text(325, 180, '[@]'),
        _text(600, 180, '[ ]'),
        _text(825, 180, '[>]'),
        _text(153, 180, '-- -- -- --'),
        _text(378, 180, '-- -- -- -- --'),
        _text(653, 180, '-- -- -- --'),
        _text(338, 208, '|'),
        _text(338, 237, '+'),
        _text(363, 237, '.. .. .. .. .. ..'),
        _text(613, 237, '+'),
        _text(613, 208, '|'),
        '</g>',
        _packet(
            '120 173;120 173;345 173;620 173;845 173;845 173;120 173;120 173',
            '0;0.43;0.52;0.62;0.73;0.76;0.85;1',
            '0;0.43;0.46;0.73;0.77;1',
            '+',
        ),
        _packet(
            '345 173;345 173;345 230;620 230;620 173;620 173;345 173;345 173',
            '0;0.51;0.565;0.65;0.7;0.74;0.84;1',
            '0;0.51;0.54;0.7;0.75;1',
            '*',
        ),
        '<g font-family="monospace" font-size="17" fill="#7d8491">',
        _text(96, 280, '[ svg + text ]'),
        _text(841, 280, '>'),
        '<text x="862" y="280" fill="#5d89bd">_',
        _animation(
            'animate',
            'fill',
            '#5d89bd;#5d89bd;#afc9e7;#5d89bd;#5d89bd',
            '0;0.74;0.78;0.89;1',
        ),
        '</text></g></svg>',
    ))
    return ''.join(parts)


DEFAULT_SVG = default_art()
