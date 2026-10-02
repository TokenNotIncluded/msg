"""Bounded, script-free SVG artwork with reproducible per-account randomness."""

import math
import random
import re
from functools import lru_cache
from hashlib import sha256
from html import escape
from xml.etree import ElementTree as ET

MAX_SVG_BYTES = 98304
SVG_NS = 'http://www.w3.org/2000/svg'


def safe_svg(raw):
    """Accept self-contained image SVGs, never executable or external content."""
    if not raw or len(raw) > MAX_SVG_BYTES:
        return None
    try:
        text = raw.decode('utf-8')
        if re.search(
            r'<!|url\s*\(|@import|javascript:|data:|https?:|//', text.replace(SVG_NS, ''), re.I
        ):
            return None
        root = ET.fromstring(text)
    except UnicodeError, ET.ParseError:
        return None
    if root.tag != '{' + SVG_NS + '}svg':
        return None
    allowed = {
        'svg',
        'g',
        'defs',
        'title',
        'desc',
        'text',
        'tspan',
        'rect',
        'circle',
        'ellipse',
        'path',
        'line',
        'polyline',
        'polygon',
        'style',
        'animate',
        'animateTransform',
        'animateMotion',
        'set',
    }
    for node in root.iter():
        if not isinstance(node.tag, str) or not node.tag.startswith('{' + SVG_NS + '}'):
            return None
        if node.tag.split('}')[-1] not in allowed:
            return None
        for key, value in node.attrib.items():
            name = key.split('}')[-1].lower()
            if name.startswith('on') or name in {'href', 'src'}:
                return None
            # Animation must not mutate attributes into active content.
            if name == 'attributename' and value.lower() not in {
                'opacity',
                'fill',
                'stroke',
                'transform',
                'x',
                'y',
                'cx',
                'cy',
                'r',
                'rx',
                'ry',
                'd',
                'visibility',
                'stroke-width',
                'font-size',
            }:
                return None
    return text


def still_svg(svg):
    root = ET.fromstring(svg)
    for parent in root.iter():
        for child in list(parent):
            if child.tag.split('}')[-1] in {'animate', 'animateTransform', 'animateMotion', 'set'}:
                parent.remove(child)
    ET.SubElement(root, '{' + SVG_NS + '}style').text = '*{animation:none!important}'
    return ET.tostring(root, encoding='unicode')


@lru_cache(maxsize=128)
def generate_svg(seed, kind, role=''):
    if kind == 'footer':
        # Keep the established ocean, including its exact seed mapping.
        rng = random.Random(int.from_bytes(sha256(str(seed).encode()).digest()[:8], 'big'))
        return _ocean(rng)
    if kind not in {'avatar', 'background'}:
        raise KeyError(kind)
    theme = role if role in {'root', 'online_ca'} else ''
    digest = sha256(
        b'msg-profile-art:v2\0'
        + kind.encode()
        + b'\0'
        + theme.encode()
        + b'\0'
        + str(seed).encode()
    ).digest()
    rng = random.Random(int.from_bytes(digest[8:16], 'big'))
    if theme == 'root':
        return _geometric_art(rng, kind, 'system-core', ('#5895b1', '#8a9aa3'))
    if theme == 'online_ca':
        return _geometric_art(rng, kind, 'system-trust', ('#aa8d49', '#9f977f'))
    return _geometric_art(rng, kind, _ART_FAMILIES[digest[0] % 8], _ART_PALETTES[digest[1] % 8])


_ART_FAMILIES = ('orbit', 'prism', 'bloom', 'spiral', 'steps', 'kite', 'weave', 'beacon')
_ART_PALETTES = (
    ('#477f9a', '#7c929d'),
    ('#a96944', '#9c8980'),
    ('#73864b', '#8d9580'),
    ('#8970a0', '#98909d'),
    ('#3d8e7d', '#809b94'),
    ('#a46c82', '#9d8c94'),
    ('#ac8946', '#9d9583'),
    ('#667e91', '#8d969d'),
)


def _geometric_art(rng, kind, family, palette):
    """A quiet ASCII mark with a distinct silhouette, rather than recolored flames.

    The existing browser grammar owns the surface: thin geometry, open space,
    one accent and monospace characters. Avatar marks stay central; wide art
    uses asymmetrical, family-specific arrangements. No account seed is emitted.
    """
    avatar = kind == 'avatar'
    width, height = (300, 320) if avatar else (1200, 360)
    ink, quiet = palette
    duration = rng.uniform(11, 19)
    motion = (
        'core-breath'
        if family == 'system-core'
        else 'trust-flow'
        if family == 'system-trust'
        else 'drift'
    )
    midpoint = (
        'transform:rotate(1deg);opacity:.72'
        if family == 'system-core'
        else 'transform:translateX(3px);opacity:.76'
        if family == 'system-trust'
        else 'transform:translateY(-3px);opacity:.72'
    )
    parts = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 {width} {height}" '
        f'data-family="{family}" preserveAspectRatio="xMidYMid {"meet" if avatar else "slice"}" role="img">',
        f'<title>Looping ASCII {family} {kind}</title>',
        '<style>text{font:13px monospace;white-space:pre;text-anchor:middle}',
        '.art-motion{opacity:.9}',
        f'@keyframes {motion}' + '{0%,100%{transform:none;opacity:.9}50%{' + midpoint + '}}',
        '@media(prefers-reduced-motion:no-preference){.art-motion{',
        f'animation:{motion} {duration:.2f}s ease-in-out infinite',
        '}}</style>',
    ]
    if avatar:
        placements = [(rng.uniform(140, 160), rng.uniform(142, 164), rng.uniform(0.83, 1.04))]
    else:
        # Wide compositions differ in topology as well as in shape and color.
        layouts = {
            'orbit': ((185, 180, 1.65), (910, 110, 0.65)),
            'prism': ((220, 200, 1.1), (630, 100, 0.72), (1070, 230, 1.35)),
            'bloom': ((95, 215, 1.35), (870, 145, 1.8)),
            'spiral': ((275, 115, 1.55), (1030, 250, 0.8)),
            'steps': ((180, 215, 1.0), (595, 185, 1.25), (1010, 140, 0.85)),
            'kite': ((210, 100, 1.1), (850, 215, 1.5)),
            'weave': ((90, 140, 1.5), (670, 220, 0.9), (1100, 100, 0.65)),
            'beacon': ((260, 215, 1.2), (920, 170, 1.6)),
            'system-core': ((270, 180, 1.55), (960, 115, 0.8)),
            'system-trust': ((230, 135, 1.4), (830, 220, 1.65)),
        }
        placements = [
            (x + rng.uniform(-45, 45), y + rng.uniform(-24, 24), scale * rng.uniform(0.9, 1.1))
            for x, y, scale in layouts[family]
        ]
    for x, y, scale in placements:
        angle = rng.uniform(-14, 14)
        parts.append(
            f'<g class="art-composition" transform="translate({x:.2f} {y:.2f}) '
            f'rotate({angle:.2f}) scale({scale:.3f})" opacity="{1 if avatar else 0.52}">'
            '<g class="art-motion">'
        )
        parts.extend(
            _system_motif(rng, family, ink, quiet, background=not avatar)
            if family.startswith('system-')
            else _art_motif(rng, family, ink, quiet)
        )
        parts.append('</g></g>')
    # Detached glyphs create a second scale without filling every empty pixel.
    for _ in range(5 if avatar else 22):
        x, y = rng.uniform(22, width - 22), rng.uniform(24, height - 24)
        parts.append(_art_text(x, y, rng.choice('.+:|-'), quiet, opacity=0.42))
    return ''.join(parts) + '</svg>'


def _art_text(x, y, glyph, color, *, opacity=0.9):
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" fill="{color}" opacity="{opacity}">{escape(glyph)}</text>'
    )


def _art_line(points, color, *, opacity=0.8, closed=False):
    tag = 'polygon' if closed else 'polyline'
    coordinates = ' '.join(f'{x:.2f},{y:.2f}' for x, y in points)
    return (
        f'<{tag} points="{coordinates}" fill="none" stroke="{color}" '
        f'stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round" opacity="{opacity}"/>'
    )


def _system_motif(rng, family, ink, quiet, *, background):
    """Role artwork is ornamental identity, never a trust or live-status claim."""
    parts = []
    if family == 'system-core':
        if background:
            points = [(rng.uniform(-110, 110), rng.uniform(-76, 76)) for _ in range(9)]
            parts.append(_art_line(sorted(points), quiet, opacity=0.5))
            for i, (x, y) in enumerate(points):
                parts.append(_art_text(x, y, '*' if i % 3 == 0 else '+', ink))
        else:
            for radius in (84, 55):
                polygon = [
                    (radius * math.cos(i * math.tau / 6), radius * math.sin(i * math.tau / 6))
                    for i in range(6)
                ]
                parts.append(_art_line(polygon, ink, closed=True))
                for x, y in polygon:
                    parts.append(_art_text(x, y + 4, '+', quiet))
            for i in range(6):
                a = i * math.tau / 6
                parts.append(
                    _art_line(
                        (
                            (math.cos(a) * 22, math.sin(a) * 22),
                            (math.cos(a) * 50, math.sin(a) * 50),
                        ),
                        quiet,
                    )
                )
            parts.append(_art_line(((-14, -14), (14, -14), (14, 14), (-14, 14)), ink, closed=True))
            parts.append(_art_text(0, 4, '*', ink))
    elif background:
        # The CA backdrop is a sparse layered lattice, separate from the key mark.
        nodes = [(-96, -56), (0, -56), (96, -56), (-48, 0), (48, 0), (-96, 56), (0, 56), (96, 56)]
        for a, b in ((0, 3), (1, 3), (1, 4), (2, 4), (3, 5), (3, 6), (4, 6), (4, 7)):
            parts.append(_art_line((nodes[a], nodes[b]), quiet))
        for x, y in nodes:
            parts.append(
                _art_line(((x, y - 8), (x + 8, y), (x, y + 8), (x - 8, y)), ink, closed=True)
            )
            parts.append(_art_text(x, y + 4, '.', ink))
    else:
        # A crisp geometric key with an open head and two deliberate teeth.
        radius = rng.uniform(32, 40)
        head = [
            (-50 + radius * math.cos(i * math.tau / 6), radius * math.sin(i * math.tau / 6))
            for i in range(6)
        ]
        parts.append(_art_line(head, ink, closed=True))
        parts.append(
            _art_line(
                (
                    (-50 + radius, -7),
                    (94, -7),
                    (94, 13),
                    (75, 13),
                    (75, 34),
                    (60, 34),
                    (60, 13),
                    (35, 13),
                    (35, 28),
                    (20, 28),
                    (20, 13),
                    (-50 + radius, 13),
                ),
                ink,
            )
        )
        parts.append(_art_text(-50, 4, '+', quiet))
        for x in (0, 20, 40, 60, 80):
            parts.append(_art_text(x, -20, '.', quiet))
    return parts


def _art_motif(rng, family, ink, quiet):
    parts = []

    def line(points, *, closed=False, muted=False):
        parts.append(_art_line(points, quiet if muted else ink, closed=closed))

    def mark(x, y, glyph='+', *, muted=False):
        parts.append(_art_text(x, y, glyph, quiet if muted else ink))

    size = rng.uniform(76, 94)
    if family == 'orbit':
        # One open orbital plane and an eccentric satellite, not a target icon.
        for radius, aspect, tilt in ((size, 0.48, -32), (size * 0.78, 0.62, 36)):
            parts.append(
                f'<ellipse rx="{radius:.2f}" ry="{radius * aspect:.2f}" '
                f'transform="rotate({tilt})" fill="none" stroke="{ink}" stroke-width="1.25"/>'
            )
        angle = rng.uniform(0.2, 1.1)
        mark(size * math.cos(angle), size * 0.48 * math.sin(angle), '*')
        mark(-size * 0.7, -size * 0.28, '+')
        for y in (-16, 0, 16):
            mark(0, y, '|')
        mark(18, 3, '.')
    elif family == 'prism':
        # Three broad facets share a single off-center vertex.
        tip, left, right, base = (0, -size), (-size, -size * 0.25), (size, -size * 0.25), (0, size)
        line((tip, right, base, left), closed=True)
        line((left, (0, size * 0.18), right))
        line((tip, (0, size * 0.18), base))
        for x, y in (tip, left, right, base):
            mark(x, y + 5, '+')
        for i in range(6):
            mark(-size * 0.48 + i * 6, -size * 0.04 + i * 9, ':', muted=True)
    elif family == 'bloom':
        petals = rng.choice((5, 6, 7))
        for i in range(petals):
            a = i * math.tau / petals
            points = [
                (math.cos(a + da) * radius, math.sin(a + da) * radius)
                for da, radius in (
                    (-0.32, 22),
                    (-0.2, size * 0.8),
                    (0, size),
                    (0.2, size * 0.8),
                    (0.32, 22),
                )
            ]
            line(points)
            mark(math.cos(a) * size * 0.68, math.sin(a) * size * 0.68 + 4, ':')
        line(((-12, -12), (12, -12), (12, 12), (-12, 12)), closed=True, muted=True)
        mark(0, 4, '*')
    elif family == 'spiral':
        turns = rng.uniform(1.6, 2.2)
        points = []
        for i in range(90):
            radius = 8 + size * 0.9 * i / 89
            angle = i / 89 * math.tau * turns
            points.append((radius * math.cos(angle), radius * math.sin(angle)))
        line(points)
        for i in range(9, 90, 12):
            mark(points[i][0], points[i][1] + 4, '+', muted=i % 2 == 0)
        mark(0, 4, '.')
    elif family == 'steps':
        # An asymmetric staircase has a broad, unmistakably rectilinear silhouette.
        for i in range(4):
            x = -size + i * size * 0.5
            top = size * 0.5 - i * size * 0.4
            line((
                (x, size * 0.75),
                (x, top),
                (x + size * 0.34, top),
                (x + size * 0.34, size * 0.75),
            ))
            for y in range(3):
                mark(x + size * 0.17, top + 15 + y * 14, '-' if i % 2 else '=', muted=y == 2)
        line(((-size - 12, size * 0.85), (size, size * 0.85)), muted=True)
    elif family == 'kite':
        # A vertical diamond chain, with two different negative-space cuts.
        for y, radius in ((-size * 0.47, size * 0.55), (size * 0.18, size * 0.37)):
            line(((0, y - radius), (radius, y), (0, y + radius), (-radius, y)), closed=True)
            mark(0, y + 4, '*')
        line(
            ((0, size * 0.55), (-size * 0.28, size * 0.86), (size * 0.08, size * 1.04)), muted=True
        )
        mark(size * 0.08, size * 1.04, ':')
    elif family == 'weave':
        # Two interleaved combs, deliberately unlike circles or radial flowers.
        for i in range(5):
            offset = -size * 0.7 + i * size * 0.35
            line(((-size, offset), (size * 0.4, offset)), muted=i % 2 == 0)
            line(((offset, -size * 0.4), (offset, size)), muted=i % 2 == 1)
            mark(size * 0.4 + 12, offset + 4, ':')
            mark(offset, -size * 0.4 - 10, '+')
    else:  # beacon
        # A branched signal stem with detached horizontal ASCII pulses.
        line(((0, size), (0, -size * 0.82)))
        for i in range(3):
            y = -size * 0.55 + i * size * 0.48
            span = size * (0.9 - i * 0.2)
            line(((-span, y + 20), (-span, y), (span, y), (span, y + 20)))
            mark(-span, y + 37, '+')
            mark(span, y + 37, ':')
        mark(0, -size * 0.9, '*')
        line(((-18, size), (18, size)), muted=True)
    return parts


def _flame(rng):
    phases = [rng.random() * math.tau for _ in range(5)]
    frames, cols, rows = 10, 28, 30
    colors = ('#ef5729', '#ffac42', '#fff0b0')
    parts = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 300 320" role="img">',
        '<title>Looping ASCII flame</title>',
        '<style>text{font:14px monospace;white-space:pre} .frame{opacity:0}',
        '.frame:first-of-type{opacity:1}',
        '@keyframes burn{0%,9.99%{opacity:1}10%,100%{opacity:0}}',
        '@media(prefers-reduced-motion:no-preference){.frame{animation:burn 2s steps(1) infinite;animation-delay:var(--phase)}}',
        '</style>',
    ]
    ramp = '.:+*xX#@'
    for frame in range(frames):
        t = frame / frames * math.tau
        parts.append(f'<g class="frame" style="--phase:-{2 - frame * 2 / frames:.2f}s">')
        for y in range(rows):
            height = 1 - y / (rows - 1)
            bands = [[' '] * cols for _ in colors]
            for x in range(cols):
                center = (cols - 1) / 2 + math.sin(height * 8 - t + phases[0]) * height * 3
                width = 10 * (1 - height) ** 0.65 + 0.6
                edge = abs(x - center) / width
                turbulence = (
                    math.sin(x * 0.7 + height * 13 - 2 * t + phases[1])
                    + math.sin(x * 0.4 - height * 17 + t + phases[2])
                ) * 0.13
                heat = 1 - edge + turbulence - height * 0.27
                if heat > 0.05 and height < 0.93 + 0.06 * math.sin(t + x):
                    band = min(2, int(max(0, heat) * 3))
                    bands[band][x] = ramp[min(7, int(max(0, heat) * 7))]
            for band, chars in enumerate(bands):
                if any(c != ' ' for c in chars):
                    parts.append(
                        f'<text x="24" y="{24 + y * 9}" fill="{colors[band]}" xml:space="preserve">{escape("".join(chars))}</text>'
                    )
        # Rising embers follow periodic orbits; no discontinuity at the loop seam.
        for i in range(8):
            a = t + phases[i % 5] + i
            x = 150 + math.sin(a) * (45 + i * 4)
            y = 60 + math.cos(a) * 36
            parts.append(
                f'<text x="{x:.1f}" y="{y:.1f}" fill="#ffac42" opacity="{0.25 + 0.2 * math.sin(a):.2f}">.</text>'
            )
        parts.append('</g>')
    parts.append('</svg>')
    return ''.join(parts)


def _stars(rng):
    parts = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 1200 360" preserveAspectRatio="xMidYMid slice">',
        '<title>Looping ASCII starfield</title><style>',
        'text{font:15px monospace;fill:#99bde0}',
        '@keyframes shimmer{0%,100%{opacity:.18;transform:translateY(0)}50%{opacity:.8;transform:translateY(-8px)}}',
        '@media(prefers-reduced-motion:no-preference){text{animation:shimmer var(--duration) ease-in-out infinite;animation-delay:var(--delay)}}',
        '</style>',
    ]
    for _ in range(180):
        x, y = rng.uniform(0, 1200), rng.uniform(0, 360)
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" opacity="{rng.uniform(0.15, 0.65):.2f}" '
            f'style="--duration:{rng.uniform(6, 18):.2f}s;--delay:-{rng.uniform(0, 18):.2f}s">{rng.choice(".+*: ")}</text>'
        )
    parts.append('</svg>')
    return ''.join(parts)


def _ocean(rng):
    phases = [rng.uniform(0, math.tau) for _ in range(3)]
    colors = ('#7bd5e5', '#47a5c6', '#287b9e')
    parts = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 1200 200" preserveAspectRatio="xMidYMid slice">',
        '<title>Looping ASCII ocean waves</title>',
        '<style>text{font:16px monospace;white-space:pre}.wave-frame{opacity:0}',
        '.wave-frame:first-of-type{opacity:1}',
        '@keyframes tide{0%,6.24%{opacity:1}6.25%,100%{opacity:0}}',
        '@media(prefers-reduced-motion:no-preference){.wave-frame{animation:tide 3.2s steps(1) infinite;animation-delay:var(--phase)}}',
        '</style>',
    ]
    for frame in range(16):
        t = frame / 16 * math.tau
        parts.append(f'<g class="wave-frame" style="--phase:-{3.2 - frame * 0.2:.2f}s">')
        for row in range(12):
            chars = []
            for col in range(120):
                surface = (
                    3
                    + 1.4 * math.sin(col * math.tau / 60 - t + phases[0])
                    + 0.8 * math.sin(col * math.tau / 120 + 2 * t + phases[1])
                )
                depth = row - surface
                ripple = math.sin(col * math.tau / 24 - 2 * t + row * 0.8 + phases[2])
                if depth < -0.6:
                    char = ' '
                elif depth < 0.6:
                    char = '~' if ripple > 0 else '_'
                elif depth < 2:
                    char = '=' if ripple > 0.4 else '~' if ripple > -0.4 else '-'
                else:
                    char = '.:-~'[int((ripple + 1) * 1.49)]
                chars.append(char)
            color = colors[min(2, row // 4)]
            parts.append(
                f'<text x="0" y="{24 + row * 14}" fill="{color}" xml:space="preserve">{escape("".join(chars))}</text>'
            )
        parts.append('</g>')
    return ''.join(parts) + '</svg>'
