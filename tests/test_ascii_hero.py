"""The homepage illustration must remain accessible and browser-only."""

from html.parser import HTMLParser

from msg.transports.home_page import _ASCII_PACKETS, _ASCII_WORDMARK, home_html


class HeroElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_ascii_hero_keeps_translatable_heading_and_ignores_decorative_frames():
    html = home_html().decode()
    parsed = HeroElements(html)
    headings = [attrs for tag, attrs in parsed.tags if tag == 'h1']
    assert headings == [{'class': 'sr-only', 'data-i18n': 'headline'}]
    assert any(
        attrs.get('class') == 'ascii-art' and attrs.get('aria-hidden') == 'true'
        for _, attrs in parsed.tags
    )
    assert sum(attrs.get('class', '').startswith('ascii-frame ') for _, attrs in parsed.tags) == 4
    assert 'data-i18n="intro"' in html
    assert '[agent] &gt;--------- [agent]' in html


def test_ascii_motion_is_bounded_and_reduced_motion_has_a_static_default():
    html = home_html().decode()
    css = html.split('<style>')[1].split('</style>')[0]
    motion = css.split('@media (prefers-reduced-motion: no-preference)')[1].split('@keyframes')[0]
    assert '.ascii-frame-0 { opacity: 1; }' in css
    assert 'animation-duration: 1.6s' in motion
    assert 'animation-iteration-count: 3' in motion
    assert 'infinite' not in css
    assert (
        'animation-name: packet-0'
        not in css.split('@media (prefers-reduced-motion: no-preference)')[0]
    )


def test_art_uses_short_ascii_lines_that_fit_a_mobile_terminal():
    for line in _ASCII_WORDMARK.splitlines() + list(_ASCII_PACKETS):
        assert line.isascii()
        assert len(line) <= 26
