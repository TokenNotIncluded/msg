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
def generate_svg(seed, kind):
    rng = random.Random(int.from_bytes(sha256(str(seed).encode()).digest()[:8], 'big'))
    return _flame(rng) if kind == 'avatar' else _stars(rng)


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
