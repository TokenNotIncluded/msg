"""The packaged default artwork follows the same limits as users' artwork."""

import xml.etree.ElementTree as ET

from msg.core.public_board_art import DEFAULT_SVG, DEFAULT_TEXT, default_art
from msg.plugins.public_board import LIMITS, validate_svg


def test_default_art_is_deterministic_and_allowed():
    assert default_art() == DEFAULT_SVG
    assert validate_svg(DEFAULT_SVG) == DEFAULT_SVG
    assert len(DEFAULT_SVG.encode()) < LIMITS['svg_bytes']
    root = ET.fromstring(DEFAULT_SVG)
    elements = list(root.iter())
    animations = [element for element in elements if element.tag.endswith('}animate')]
    assert len(elements) <= LIMITS['elements']
    assert len(animations) == 1
    assert animations[0].get('dur') == '12s'
    assert root.get('viewBox') == '0 0 960 300'
    assert not any(element.tag.endswith('}rect') for element in elements)


def test_default_art_has_only_ascii_text_and_a_short_invitation():
    root = ET.fromstring(DEFAULT_SVG)
    for element in root.iter():
        if element.text:
            assert element.text.isascii()
    assert len(DEFAULT_TEXT) <= 20
    assert '下一位' in DEFAULT_TEXT
