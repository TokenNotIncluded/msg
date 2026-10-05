"""Small browser-only heading index; no extra content in agent projections."""

import re
from base64 import b64encode
from hashlib import sha256
from html import escape, unescape

CSS = """
.document-outline { position: fixed; inset: 22vh 24px auto auto; z-index: 5;
  width: 44px; height: 56vh; display: block; transition: width .32s ease;
  color: var(--muted); }
.document-outline::before { content: ''; position: absolute; right: 20px;
  top: 8px; bottom: 8px; width: 2px; background: var(--line); border-radius: 2px; }
.outline-toggle { position: absolute; inset: 0; border: 0; padding: 0;
  background: transparent; cursor: pointer; width: 100%; }
.outline-toggle:after { content: ''; position: absolute; right: 18px;
  top: var(--progress, 0%); width: 6px; height: 28px; max-height: 10%;
  border-radius: 4px; background: var(--accent); transition: top .16s ease; }
.outline-list { position: relative; margin: 0; padding: 28px 14px 28px 8px;
  list-style: none; max-height: 100%; overflow-y: auto; overscroll-behavior: contain;
  scrollbar-width: thin; scrollbar-color: var(--line) transparent;
  opacity: 0; visibility: hidden; transform: translateX(12px) scale(.96);
  transform-origin: right center; transition: opacity .24s, transform .32s ease;
  mask-image: linear-gradient(transparent, #000 22px, #000 calc(100% - 22px), transparent); }
.document-outline:hover, .document-outline:focus-within, .document-outline.is-open {
  width: 260px; }
.document-outline:hover .outline-list, .document-outline:focus-within .outline-list,
.document-outline.is-open .outline-list { opacity: 1; visibility: visible;
  transform: none; }
.document-outline:hover::before, .document-outline:focus-within::before,
.document-outline.is-open::before, .document-outline:hover .outline-toggle:after,
.document-outline:focus-within .outline-toggle:after,
.document-outline.is-open .outline-toggle:after { opacity: 0; }
.outline-list li { position: relative; padding-left: calc(12px + var(--depth) * 12px);
  transform: translateX(var(--wave, 0px)) scale(var(--scale, 1)); transform-origin: left center; }
.outline-list li:before { content: ''; position: absolute; left: 0; top: 50%;
  width: var(--rung, 5px); height: 1px; background: var(--line); }
.outline-list a { display: block; min-height: 36px; padding: 7px 10px;
  font-size: 12px; line-height: 1.5; color: var(--muted); text-decoration: none;
  background: var(--bg); border-radius: 5px; transition: color .18s; }
.outline-list a:hover, .outline-list a[aria-current=location] { color: var(--accent); }
.outline-list a[aria-current=location] { font-weight: 600; }
.prose :is(h1,h2,h3,h4,h5,h6)[id] { scroll-margin-top: 32px; }
@media (min-width: 960px) {
  .page-document.has-outline main { max-width: 1128px; padding-right: 310px; }
  .document-outline { right: max(24px, calc((100vw - 1128px) / 2 + 28px)); }
}
@media (max-width: 959px) { .document-outline { display: none; } }
@media (prefers-reduced-motion: reduce) {
  .document-outline, .outline-list, .outline-toggle:after { transition: none; }
  .outline-list li { transform: none; }
}
"""

SCRIPT = r"""(() => {
  const rail = document.querySelector('.document-outline');
  if (!rail) return;
  const list = rail.querySelector('.outline-list');
  const button = rail.querySelector('.outline-toggle');
  const links = [...list.querySelectorAll('a')];
  const headings = links.map(a => document.getElementById(decodeURIComponent(a.hash.slice(1))));
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const visible = matchMedia('(min-width: 960px)');
  let frame = 0, hovering = false, active = -1, phase = 0, lastTime = 0;
  const expanded = () => hovering || rail.classList.contains('is-open') || rail.contains(document.activeElement);
  function update(time) {
    frame = 0;
    if (!visible.matches || document.hidden) { lastTime = 0; return; }
    button.setAttribute('aria-expanded', String(expanded()));
    let current = 0;
    headings.forEach((h, i) => { if (h && h.getBoundingClientRect().top <= innerHeight * .25) current = i; });
    if (current !== active) {
      active = current;
      links.forEach((a, i) => i === active ? a.setAttribute('aria-current', 'location') : a.removeAttribute('aria-current'));
      if (!hovering && !rail.contains(document.activeElement)) {
        const entry = links[active];
        list.scrollTop = Math.max(0, entry.offsetTop - list.clientHeight / 2);
      }
    }
    const range = document.documentElement.scrollHeight - innerHeight;
    rail.style.setProperty('--progress', `${range > 0 ? Math.min(90, scrollY / range * 90) : 0}%`);
    if (expanded() && !reduced.matches) {
      phase += Math.min(50, lastTime ? time - lastTime : 0) * .0007;
      links.forEach((a, i) => {
        const wave = Math.sin(i * .65 + phase + list.scrollTop * .012);
        a.parentElement.style.setProperty('--wave', `${wave * 7}px`);
        a.parentElement.style.setProperty('--rung', `${8 + (wave + 1) * 7}px`);
        a.parentElement.style.setProperty('--scale', `${.97 + Math.cos(i * .65 + phase) * .03}`);
      });
      lastTime = time;
      schedule();
    } else { lastTime = 0; }
  }
  function schedule() { if (!frame) frame = requestAnimationFrame(update); }
  rail.addEventListener('pointerenter', () => { hovering = true; schedule(); });
  rail.addEventListener('pointerleave', e => {
    hovering = false;
    if (e.pointerType === 'mouse') {
      rail.classList.remove('is-open');
      if (document.activeElement === button) button.blur();
    }
    schedule();
  });
  rail.addEventListener('focusin', schedule);
  rail.addEventListener('focusout', schedule);
  button.addEventListener('click', () => {
    const open = rail.classList.toggle('is-open');
    button.setAttribute('aria-expanded', String(open)); schedule();
  });
  rail.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      rail.classList.remove('is-open'); button.setAttribute('aria-expanded', 'false');
      button.blur(); hovering = false; schedule();
    }
  });
  links.forEach(a => a.addEventListener('click', e => {
    const target = document.getElementById(decodeURIComponent(a.hash.slice(1)));
    if (!target || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    history.pushState(null, '', a.hash);
    target.scrollIntoView({behavior: reduced.matches ? 'instant' : 'smooth', block: 'start'});
    target.setAttribute('tabindex', '-1'); target.focus({preventScroll: true});
    rail.classList.remove('is-open'); button.setAttribute('aria-expanded', 'false'); schedule();
  }));
  addEventListener('scroll', schedule, {passive: true});
  addEventListener('resize', schedule, {passive: true});
  list.addEventListener('scroll', schedule, {passive: true});
  document.addEventListener('visibilitychange', schedule);
  reduced.addEventListener('change', schedule);
  schedule();
})();"""
HASH = b64encode(sha256(SCRIPT.encode()).digest()).decode()


def outline_html(body):
    """Anchor rendered headings, preserving existing IDs and escaping index labels."""
    original = body
    entries = []
    used = set(re.findall(r'\bid="([^"]+)"', body))

    def heading(match):
        level, attributes, content = match.groups()
        attributes = attributes or ''
        label = unescape(re.sub(r'<[^>]*>', '', content)).strip()
        if not label:
            return match.group()
        existing = re.search(r'\bid="([^"]+)"', attributes)
        if existing:
            anchor = unescape(existing[1])
        else:
            base = 'section-' + (
                re.sub(r'[^\w-]+', '-', label.lower()).strip('-')[:64] or 'heading'
            )
            anchor = base
            serial = 2
            while anchor in used:
                anchor = f'{base}-{serial}'
                serial += 1
            used.add(anchor)
            attributes += f' id="{escape(anchor, quote=True)}"'
        entries.append((int(level), anchor, label))
        return f'<h{level}{attributes}>{content}</h{level}>'

    body = re.sub(r'<h([1-6])(\s[^>]*)?>(.*?)</h\1>', heading, body, flags=re.DOTALL)
    if len(entries) < 2:
        return original, ''
    minimum = min(level for level, _, _ in entries)
    from urllib.parse import quote

    items = ''.join(
        f'<li style="--depth:{level - minimum}"><a href="#{quote(anchor, safe="-_")}">{escape(label)}</a></li>'
        for level, anchor, label in entries
    )
    return body, (
        '<nav class="document-outline" aria-label="Document outline / 正文大纲">'
        '<button class="outline-toggle" type="button" aria-expanded="false" aria-controls="document-outline-list">'
        '<span class="sr-only">Document outline / 正文大纲</span></button>'
        f'<ol id="document-outline-list" class="outline-list">{items}</ol></nav>'
    )
