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


def _animation(tag, attribute, values, times, **attributes):
    """Keep every layer on one clock, including the first and last still frame."""
    extras = ''.join(f' {name}="{value}"' for name, value in attributes.items())
    return (
        f'<{tag} attributeName="{attribute}" values="{values}" '
        f'keyTimes="{times}" dur="12s" repeatCount="indefinite"{extras}/>'
    )


def default_art():
    """ASCII lines gather, carry a packet, then spring back into the same word."""
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 300">',
        '<title>MSG in ASCII</title>',
        '<desc>ASCII lines gather into MSG. A packet passes through the letters, '
        'which ripple and return. Shared SVG and text.</desc>',
        '<g font-family="monospace" font-size="36" fill="#7d8491">',
    ]
    for letter, rows in enumerate(_LETTERS):
        parts.append('<g>')
        # A brief, local blue response follows the packet rather than flashing
        # the entire word. The original grey remains the resting/still state.
        response = 0.32 + letter * 0.19
        parts.append(
            _animation(
                'animate',
                'fill',
                '#7d8491;#7d8491;#89a7cb;#7d8491;#7d8491',
                f'0;{response - 0.025:g};{response + 0.05:g};{response + 0.17:g};1',
            )
        )
        for row, glyph in enumerate(rows):
            parts.append('<g>')
            # Rows separate by less than a character cell during gathering.
            # The moving packet then tugs alternate rows; a smaller opposite
            # displacement makes their return feel like a damped spring.
            sign = 1 if row % 2 == 0 else -1
            gather_x = sign * (12 + row * 2)
            gather_y = (row - 2.5) * 3
            arrival = response + row * 0.012
            parts.append(
                _animation(
                    'animateTransform',
                    'transform',
                    f'0 0;{gather_x:g} {gather_y:g};0 0;0 0;'
                    f'{sign * 15} {sign * 3};{-sign * 4} {-sign};0 0;0 0',
                    f'0;0.035;{0.12 + row * 0.008:g};{arrival - 0.025:g};'
                    f'{arrival + 0.035:g};{arrival + 0.085:g};{arrival + 0.15:g};1',
                    type='translate',
                    calcMode='spline',
                    keySplines=';'.join(('0.2 0 0.3 1',) * 7),
                )
            )
            for run in re.finditer(r'\S+', glyph):
                x = 111 + (letter * 13 + run.start()) * 21.6
                parts.append(f'<text x="{x:g}" y="{69 + row * 33}">{escape(run.group())}</text>')
            parts.append('</g>')
        parts.append('</g>')
    parts.extend((
        '</g>',
        # A little train of ASCII punctuation is the information packet.
        # One group moves all of it; it never needs a path, filter or script.
        '<g font-family="monospace" font-size="19" fill="#89a7cb" opacity="0">',
        _animation(
            'animateTransform',
            'transform',
            '72 151;72 151;135 151;260 118;415 151;540 184;690 151;815 151;857 275;72 151;72 151',
            '0;0.22;0.28;0.4;0.49;0.59;0.68;0.77;0.84;0.94;1',
            type='translate',
            calcMode='spline',
            keySplines=';'.join(('0.35 0 0.65 1',) * 10),
        ),
        _animation('animate', 'opacity', '0;0;1;1;0;0', '0;0.22;0.25;0.82;0.87;1'),
        '<text x="-35" y="5" fill-opacity="0.24">.</text>',
        '<text x="-21" y="5" fill-opacity="0.48">:</text>',
        '<text x="-5" y="5">+</text>',
        '</g>',
        '<g font-family="monospace" font-size="17" fill="#7d8491">',
        '<text x="111" y="280">[</text>',
        '<text x="136" y="280">svg + text</text>',
        '<text x="254" y="280">]</text>',
        '<text x="829" y="280">&gt;</text>',
        '<text x="851" y="280" fill="#5d89bd">_',
        _animation(
            'animate',
            'fill',
            '#5d89bd;#5d89bd;#b6cce7;#5d89bd;#5d89bd',
            '0;0.82;0.86;0.92;1',
        ),
        '</text></g></svg>',
    ))
    return ''.join(parts)


DEFAULT_SVG = default_art()
