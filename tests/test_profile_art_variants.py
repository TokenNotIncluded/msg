"""Default identity art varies in silhouette, palette and composition, safely."""

import re
from hashlib import sha256
from xml.etree import ElementTree as ET

import pytest

from msg.core.profile_art import MAX_SVG_BYTES, generate_svg, safe_svg, still_svg

NS = '{http://www.w3.org/2000/svg}'
SEEDS = tuple('u_' + format(number, '032x') for number in range(24))


def colors(root):
    return tuple(
        sorted({
            value
            for node in root.iter()
            for key, value in node.attrib.items()
            if key in {'fill', 'stroke'} and value.startswith('#')
        })
    )


@pytest.mark.parametrize('kind', ('avatar', 'background'))
def test_twenty_four_identities_change_actual_shape_color_and_layout(kind):
    families, palettes, layouts, drawings = set(), set(), set(), set()
    for seed in SEEDS:
        svg = generate_svg(seed, kind)
        root = ET.fromstring(svg)
        families.add(root.attrib['data-family'])
        palettes.add(colors(root))
        layouts.add(
            tuple(
                node.attrib['transform']
                for node in root.findall(NS + 'g')
                if node.attrib.get('class') == 'art-composition'
            )
        )
        drawings.add(svg)
        assert len(svg.encode()) < MAX_SVG_BYTES
        assert safe_svg(svg.encode()) == svg
        assert root.find(NS + 'text') is not None
        assert seed not in svg
        assert all((node.text or '').isascii() for node in root.iter(NS + 'text'))
        # A new process must reproduce the same image, not merely hit a cache.
        generate_svg.cache_clear()
        assert generate_svg(seed, kind) == svg
    assert len(families) >= 6
    assert len(palettes) >= 6
    assert len(layouts) == len(drawings) == 24


def test_families_use_different_geometry_and_avatar_background_are_independent():
    signatures, pairings = {}, []
    for seed in SEEDS:
        avatar = ET.fromstring(generate_svg(seed, 'avatar'))
        background = ET.fromstring(generate_svg(seed, 'background'))
        family = avatar.attrib['data-family']
        tags = tuple(node.tag for node in avatar.find(NS + 'g').iter())
        signatures.setdefault(family, tags)
        pairings.append((family, background.attrib['data-family']))
        assert avatar.attrib['viewBox'] == '0 0 300 320'
        assert background.attrib['viewBox'] == '0 0 1200 360'
        assert avatar.attrib['preserveAspectRatio'].endswith('meet')
        assert background.attrib['preserveAspectRatio'].endswith('slice')
    assert len(set(signatures.values())) == len(signatures)
    assert sum(a != b for a, b in pairings) >= 12


@pytest.mark.parametrize('role', ('', 'root', 'online_ca'))
@pytest.mark.parametrize('kind', ('avatar', 'background'))
def test_motion_is_gentle_looping_opt_in_and_still_is_inert(role, kind):
    svg = generate_svg('a-private-unpublished-seed', kind, role=role)
    assert 'a-private-unpublished-seed' not in svg
    assert safe_svg(svg.encode()) == svg
    assert len(svg.encode()) < MAX_SVG_BYTES
    style = ET.fromstring(svg).find(NS + 'style').text
    assert '0%,100%{transform:none;opacity:.9}' in style
    assert '@media(prefers-reduced-motion:no-preference)' in style
    assert re.search(r'animation:[a-z-]+ 1[1-9]\.\d+s ease-in-out infinite', style)
    assert 'translateY(-3px)' in style or 'translateX(3px)' in style or 'rotate(1deg)' in style
    still = still_svg(svg)
    assert safe_svg(still.encode()) == still
    assert 'animation:none!important' in still
    assert not any(
        node.tag.removeprefix(NS) in {'animate', 'animateTransform', 'animateMotion', 'set'}
        for node in ET.fromstring(still).iter()
    )


def test_system_roles_have_distinct_themes_without_guessing_handles():
    for kind in ('avatar', 'background'):
        normal = generate_svg('@root', kind)
        root = generate_svg('@root', kind, role='root')
        ca = generate_svg('@root', kind, role='online_ca')
        assert len({normal, root, ca}) == 3
        assert ET.fromstring(normal).attrib['data-family'] not in {'system-core', 'system-trust'}
        assert ET.fromstring(root).attrib['data-family'] == 'system-core'
        assert ET.fromstring(ca).attrib['data-family'] == 'system-trust'
        assert '@keyframes core-breath' in root
        assert '@keyframes trust-flow' in ca
        assert colors(ET.fromstring(root)) != colors(ET.fromstring(ca))
        generate_svg.cache_clear()
        assert generate_svg('@root', kind, role='root') == root
        assert generate_svg('@root', kind, role='online_ca') == ca
        assert generate_svg('@root', kind, role='unrecognized') == normal
        assert generate_svg('@online-ca', kind) != generate_svg(
            '@online-ca', kind, role='online_ca'
        )


@pytest.mark.parametrize(
    ('seed', 'expected'),
    (
        (0, '38e91d81d5bdc2f841b5a1de5e63cf5347c1606736768341567a04d46aabe48d'),
        ('stable-footer', '0a994c421df426b5fc441582c106d2e3707672f1daf2e8fbc098e5d97780b205'),
    ),
)
def test_ocean_footer_keeps_previous_bytes_for_every_role(seed, expected):
    for role in ('', 'root', 'online_ca'):
        assert sha256(generate_svg(seed, 'footer', role=role).encode()).hexdigest() == expected


def test_unrecognized_kind_still_fails():
    with pytest.raises(KeyError):
        generate_svg('any-account', 'script')
