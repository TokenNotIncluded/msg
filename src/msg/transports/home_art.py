"""A small token cloud assembles the wordmark without canvas or dependencies."""

from base64 import b64encode
from hashlib import sha256
from html import escape

_GLYPHS = (
    ('10001', '11011', '10101', '10001', '10001', '10001', '10001'),
    ('01111', '10000', '10000', '01110', '00001', '00001', '11110'),
    ('01110', '10001', '10000', '10111', '10001', '10001', '01110'),
)
_TOKENS = ('01', '{}', 'msg', '/', '[]', 'if', '::', 'io', '<>', '0x', 'ai', '#')


def token_art():
    particles = []
    for letter, rows in enumerate(_GLYPHS):
        for row, pixels in enumerate(rows):
            for column, pixel in enumerate(pixels):
                if pixel != '1':
                    continue
                index = len(particles)
                x, y = 94 + (letter * 6 + column) * 32, 56 + row * 26
                # Stable scatter avoids layout shifts and keeps the cloud inside its viewBox.
                scatter_x = 22 + (index * 137 + 39) % 672
                scatter_y = 18 + (index * 83 + 17) % 240
                style = (
                    f'--dx:{scatter_x - x}px;--dy:{scatter_y - y}px;'
                    f'--turn:{(index * 11) % 31 - 15}deg;'
                    f'--phase:{-4 - (index % 9) * 0.045:.3f}s'
                )
                tone = ' token-accent' if index % 7 == 0 else ''
                particles.append(
                    f'<text class="token{tone}" x="{x}" y="{y}" style="{style}">'
                    + escape(_TOKENS[index % len(_TOKENS)])
                    + '</text>'
                )
    return (
        '<div class="token-art" id="token-art">'
        '<svg class="token-cloud" viewBox="0 0 720 280" aria-hidden="true" focusable="false">'
        + ''.join(particles)
        + '</svg><button class="token-pause" type="button" aria-pressed="false" '
        'aria-label="Pause animation / 暂停动画" title="Pause animation / 暂停动画" hidden>'
        '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">'
        '<path class="pause-mark" d="M5 3v10M11 3v10" fill="none" stroke="currentColor" stroke-width="1.5"/>'
        '<path class="play-mark" d="m5 3 8 5-8 5z" fill="currentColor"/>'
        '</svg></button></div>'
    )


TOKEN_HERO = token_art()
HERO_SCRIPT = r"""(() => {
  const art = document.getElementById('token-art');
  if (!art) return;
  const button = art.querySelector('button');
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  let visible = false, paused = false;
  const sync = () => {
    art.dataset.running = String(visible && !document.hidden && !paused && !motion.matches);
    button.hidden = motion.matches;
    button.setAttribute('aria-pressed', String(paused));
    const label = paused ? 'Resume animation / 继续动画' : 'Pause animation / 暂停动画';
    button.setAttribute('aria-label', label);
    button.title = label;
  };
  button.addEventListener('click', () => { paused = !paused; sync(); });
  document.addEventListener('visibilitychange', sync);
  motion.addEventListener('change', sync);
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(entries => { visible = entries[0].isIntersecting; sync(); },
      { threshold: 0.1 }).observe(art);
  }
  sync();
})();"""
HERO_HASH = b64encode(sha256(HERO_SCRIPT.encode()).digest()).decode()
HERO_TAG = '<script>' + HERO_SCRIPT + '</script>'
