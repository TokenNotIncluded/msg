"""The homepage illustration must remain accessible and browser-only."""

from html.parser import HTMLParser

from msg.transports.home_art import HERO_HASH, HERO_SCRIPT
from msg.transports.home_page import HOME_BROWSER_HEADERS, home_html


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
        attrs.get('class') == 'token-cloud' and attrs.get('aria-hidden') == 'true'
        for _, attrs in parsed.tags
    )
    assert 'data-i18n="intro"' in html
    assert any(
        tag == 'button' and attrs.get('aria-pressed') == 'false' for tag, attrs in parsed.tags
    )
    assert f"'sha256-{HERO_HASH}'" in HOME_BROWSER_HEADERS['Content-Security-Policy']


def test_motion_is_opt_in_and_pauses_outside_the_visible_page():
    html = home_html().decode()
    css = html.split('<style>')[1].split('</style>')[0]
    base, motion = css.split('@media (prefers-reduced-motion: no-preference)')
    assert 'animation: token-assemble' not in base
    assert 'animation-play-state: paused' in motion
    assert '.token-art[data-running=true]' in motion
    assert 'IntersectionObserver' in HERO_SCRIPT and '!document.hidden' in HERO_SCRIPT
    assert '!motion.matches' in HERO_SCRIPT


def test_particles_are_bounded_and_art_uses_a_mobile_viewbox():
    parsed = HeroElements(home_html().decode())
    particles = [attrs for tag, attrs in parsed.tags if tag == 'text']
    assert 40 <= len(particles) < 120
    assert all(0 < int(p['x']) < 720 and 0 < int(p['y']) < 280 for p in particles)
    assert any(tag == 'svg' and attrs.get('viewbox') == '0 0 720 280' for tag, attrs in parsed.tags)
