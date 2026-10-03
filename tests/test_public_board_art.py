"""The packaged default artwork follows the same limits as users' artwork."""

import re
import xml.etree.ElementTree as ET

from msg.core.public_board_art import DEFAULT_SVG, DEFAULT_TEXT, default_art
from msg.plugins.public_board import LIMITS, validate_svg


def test_default_art_is_deterministic_and_allowed():
    assert default_art() == DEFAULT_SVG
    assert validate_svg(DEFAULT_SVG) == DEFAULT_SVG
    assert len(DEFAULT_SVG.encode()) < LIMITS['svg_bytes']
    root = ET.fromstring(DEFAULT_SVG)
    elements = list(root.iter())
    animations = [
        element
        for element in elements
        if element.tag.split('}')[-1] in {'animate', 'animateTransform'}
    ]
    assert len(elements) <= LIMITS['elements']
    assert 0 < len(animations) <= LIMITS['animations']
    assert {animation.get('dur') for animation in animations} == {'12s'}
    assert {animation.get('repeatCount') for animation in animations} == {'indefinite'}
    for animation in animations:
        values = animation.get('values').split(';')
        times = list(map(float, animation.get('keyTimes').split(';')))
        assert values[0] == values[-1]
        assert len(values) == len(times)
        assert times[0] == 0 and times[-1] == 1 and times == sorted(times)
        if animation.get('calcMode') == 'spline':
            splines = animation.get('keySplines').split(';')
            assert len(splines) == len(values) - 1
            assert all(
                len(control.split()) == 4
                and all(0 <= float(value) <= 1 for value in control.split())
                for control in splines
            )
    assert root.get('viewBox') == '0 0 960 300'
    assert not any(element.tag.endswith('}rect') for element in elements)


def test_default_art_has_only_ascii_text_and_a_short_invitation():
    root = ET.fromstring(DEFAULT_SVG)
    for element in root.iter():
        if element.text:
            assert element.text.isascii()
    assert len(DEFAULT_TEXT) <= 20
    assert '下一位' in DEFAULT_TEXT


def test_animation_starts_at_the_still_state():
    root = ET.fromstring(DEFAULT_SVG)

    def visit(element, inherited):
        state = {**inherited, **element.attrib}
        for animation in element:
            if animation.tag.split('}')[-1] == 'animate':
                attribute = animation.get('attributeName')
                assert animation.get('values').split(';')[0] == state[attribute]
            elif (
                animation.tag.split('}')[-1] == 'animateTransform'
                and float(state.get('opacity', 1)) != 0
            ):
                kind = animation.get('type')
                first = list(map(float, animation.get('values').split(';')[0].split()))
                static = element.get('transform')
                if static:
                    match = re.fullmatch(rf'{kind}\((.*?)\)', static)
                    assert match is not None
                    assert first == list(map(float, match.group(1).split()))
                else:
                    assert first == ({'translate': [0, 0], 'scale': [1]}[kind])
            else:
                visit(animation, state)

    visit(root, {})
