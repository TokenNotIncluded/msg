"""The homepage illustration must remain accessible and browser-only."""

from html.parser import HTMLParser

from msg.transports.home_art import HERO_SCRIPT, TOKEN_HERO
from msg.transports.home_page import HOME_BROWSER_HEADERS, home_html
from msg.transports.public_board import HASH as PUBLIC_BOARD_HASH


class HeroElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_shared_home_heading_and_isolated_art_remain_accessible():
    html = home_html().decode()
    parsed = HeroElements(html)
    headings = [attrs for tag, attrs in parsed.tags if tag == 'h1']
    assert headings == [{}]
    assert '公共栏 / Shared board' in html
    assert 'class="token-cloud"' not in html
    images = [attrs for tag, attrs in parsed.tags if tag == 'img']
    assert any(
        attrs.get('src', '').startswith('/_public-board/art.svg')
        and attrs.get('alt')
        and attrs.get('width') == '960'
        and attrs.get('height') == '300'
        for attrs in images
    )
    assert any(tag == 'button' and 'data-pause' in attrs for tag, attrs in parsed.tags)
    assert f"'sha256-{PUBLIC_BOARD_HASH}'" in HOME_BROWSER_HEADERS['Content-Security-Policy']


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
    parsed = HeroElements(TOKEN_HERO)
    particles = [attrs for tag, attrs in parsed.tags if tag == 'text']
    assert 40 <= len(particles) < 120
    assert all(0 < int(p['x']) < 720 and 0 < int(p['y']) < 280 for p in particles)
    assert any(tag == 'svg' and attrs.get('viewbox') == '0 0 720 280' for tag, attrs in parsed.tags)
