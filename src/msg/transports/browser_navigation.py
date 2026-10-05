"""Quick-jump dialog and title continuity between browser pages.

The jump list offers only links the page already renders plus fixed public
routes. Title continuity names exactly one followed link per navigation so a
cross-document view transition can pair it with the post page heading.
"""

from base64 import b64encode
from hashlib import sha256

SCRIPT = r"""(() => {
const titleLinks = '.page-home .posts .post-title a, .search-results h2 a';
const sameDocument = (href, target) => {
  try { const a = new URL(href, location.href), b = new URL(target); return a.origin === b.origin && a.pathname === b.pathname; }
  catch { return false; }
};
const nameTitle = target => {
  const link = target && [...document.querySelectorAll(titleLinks)].find(item => sameDocument(item.getAttribute('href'), target));
  if (link) link.style.viewTransitionName = 'msg-title';
  return link;
};
addEventListener('pageswap', event => {
  if (!event.viewTransition || !event.activation?.entry?.url) return;
  const link = nameTitle(event.activation.entry.url);
  if (link) event.viewTransition.finished.finally(() => link.style.removeProperty('view-transition-name'));
});
addEventListener('pagereveal', event => {
  if (!event.viewTransition || !window.navigation?.activation?.from?.url) return;
  const link = nameTitle(navigation.activation.from.url);
  if (link) event.viewTransition.finished.finally(() => link.style.removeProperty('view-transition-name'));
});
if (!window.HTMLDialogElement) return;
const zh = () => (document.documentElement.lang || '').toLowerCase().startsWith('zh');
const words = {
  en: {open: 'Jump to', placeholder: 'Jump to a page, or type to search…', search: 'Search', empty: 'No matching page', hint: '↑↓ select · Enter open · Esc close', label: 'Quick jump', page: 'On this page',
    core: [['Home', '/'], ['Search', '/search'], ['Now', '/now'], ['Terminal', '/terminal'], ['Feed', '/feed'], ['Topics', '/topics'], ['Rules', '/_rules']]},
  zh: {open: '跳转', placeholder: '跳转到页面，或输入关键词搜索…', search: '搜索', empty: '没有匹配的页面', hint: '↑↓ 选择 · Enter 打开 · Esc 关闭', label: '快速跳转', page: '本页',
    core: [['首页', '/'], ['搜索', '/search'], ['现场', '/now'], ['终端', '/terminal'], ['动态', '/feed'], ['主题', '/topics'], ['规则', '/_rules']]},
};
const t = () => words[zh() ? 'zh' : 'en'];
const clean = node => (node.textContent || '').replace(/\s+/g, ' ').trim();
const collect = () => {
  const seen = new Set(), found = [];
  const push = (label, path, group) => {
    if (!label || seen.has(path)) return;
    seen.add(path); found.push({label, path, group});
  };
  const add = (selector, group) => document.querySelectorAll(selector).forEach(link => {
    let url;
    try { url = new URL(link.getAttribute('href') || '', location.href); } catch { return; }
    if (url.origin !== location.origin || url.pathname === location.pathname && url.hash) return;
    push(clean(link), url.pathname + url.search, group);
  });
  document.querySelectorAll('#content :is(h2, h3)[id]').forEach(heading => push(clean(heading), '#' + heading.id, 'page'));
  add('.site-header nav a', 'nav');
  add('.account-menu-links a, .account nav a', 'account');
  t().core.forEach(([label, path]) => push(label, path, 'nav'));
  add('footer nav a', 'resource');
  return found;
};
const score = (item, query) => {
  if (!query) return 1;
  const hay = (item.label + ' ' + item.path).toLowerCase();
  if (hay.includes(query)) return 100 - hay.indexOf(query);
  let at = 0;
  for (const ch of query) { at = hay.indexOf(ch, at); if (at < 0) return 0; at += 1; }
  return 10;
};
let dialog, input, list, status, hint, items = [], shown = [], active = 0;
const icons = {
  search: 'M7 12A5 5 0 1 0 7 2a5 5 0 0 0 0 10Zm7 2-3.5-3.5',
  nav: 'M3 8h10M9 4l4 4-4 4',
  account: 'M8 8a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Zm-5 5.5c.6-2.3 2.6-3.5 5-3.5s4.4 1.2 5 3.5',
  page: 'M6 2 4.5 14M11.5 2 10 14M2.5 5.5h11M2 10.5h11',
  resource: 'M4 2h5.5L12 4.5V14H4ZM9 2v3h3',
};
const svg = (d) => {
  const ns = 'http://www.w3.org/2000/svg', icon = document.createElementNS(ns, 'svg'), path = document.createElementNS(ns, 'path');
  icon.setAttribute('viewBox', '0 0 16 16'); icon.setAttribute('aria-hidden', 'true');
  path.setAttribute('d', d); icon.append(path); return icon;
};
const render = () => {
  const query = input.value.trim().toLowerCase();
  shown = items.map(item => [score(item, query), item]).filter(([s]) => s > 0)
    .sort((a, b) => b[0] - a[0]).map(([, item]) => item);
  if (query) shown.unshift({label: t().search + ' “' + input.value.trim() + '”', path: '/search?q=' + encodeURIComponent(input.value.trim()), group: 'search'});
  active = Math.min(active, Math.max(0, shown.length - 1));
  list.replaceChildren(...shown.map((item, index) => {
    const option = document.createElement('a');
    option.href = item.path; option.id = 'msg-jump-' + index; option.className = 'jump-item';
    option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(index === active));
    option.tabIndex = -1;
    const label = document.createElement('span'); label.className = 'jump-label'; label.textContent = item.label;
    const path = document.createElement('span'); path.className = 'jump-path'; path.textContent = item.group === 'page' ? t().page : item.path;
    option.append(svg(icons[item.group] || icons.nav), label, path);
    option.addEventListener('pointermove', () => { if (active !== index) { active = index; mark(); } });
    option.addEventListener('click', () => { if (item.group === 'page') dialog.close(); });
    return option;
  }));
  status.textContent = shown.length ? '' : t().empty;
  mark();
};
const mark = () => {
  list.querySelectorAll('.jump-item').forEach((option, index) => option.setAttribute('aria-selected', String(index === active)));
  const current = list.children[active];
  input.setAttribute('aria-activedescendant', current ? current.id : '');
  current?.scrollIntoView({block: 'nearest'});
};
const build = () => {
  dialog = document.createElement('dialog'); dialog.className = 'jump';
  const field = document.createElement('div'); field.className = 'jump-field';
  input = document.createElement('input');
  Object.assign(input, {type: 'text', autocomplete: 'off', spellcheck: false});
  input.setAttribute('role', 'combobox'); input.setAttribute('aria-expanded', 'true');
  input.setAttribute('aria-controls', 'msg-jump-list'); input.setAttribute('aria-autocomplete', 'list');
  field.append(svg(icons.search), input);
  list = document.createElement('div'); list.id = 'msg-jump-list'; list.className = 'jump-list'; list.setAttribute('role', 'listbox');
  status = document.createElement('p'); status.className = 'jump-status'; status.setAttribute('role', 'status');
  hint = document.createElement('p'); hint.className = 'jump-hint'; hint.setAttribute('aria-hidden', 'true');
  dialog.append(field, list, status, hint);
  input.addEventListener('input', () => { active = 0; render(); });
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (shown.length) { active = (active + (event.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length; mark(); }
    } else if (event.key === 'Enter' && !event.isComposing && shown[active]) {
      event.preventDefault();
      if (shown[active].group === 'page') dialog.close();
      location.assign(shown[active].path);
    }
  });
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  document.body.append(dialog);
};
const open = () => {
  if (!dialog) build();
  if (dialog.open) { dialog.close(); return; }
  const text = t();
  dialog.setAttribute('aria-label', text.label); input.setAttribute('aria-label', text.label);
  input.placeholder = text.placeholder; hint.textContent = text.hint;
  items = collect(); input.value = ''; active = 0; render();
  dialog.showModal(); input.focus();
};
const header = document.querySelector('.site-header');
if (header) {
  const trigger = document.createElement('button');
  trigger.type = 'button'; trigger.className = 'jump-trigger';
  const label = document.createElement('span'); label.className = 'jump-trigger-label';
  const key = document.createElement('kbd'); key.textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl K';
  const name = () => { label.textContent = t().open; trigger.setAttribute('aria-label', t().label); };
  name();
  new MutationObserver(name).observe(document.documentElement, {attributes: true, attributeFilter: ['lang']});
  trigger.append(svg(icons.search), label, key);
  trigger.setAttribute('aria-keyshortcuts', 'Control+K Meta+K /');
  trigger.addEventListener('click', open);
  header.querySelector('.brand')?.after(trigger);
}
document.addEventListener('keydown', event => {
  const typing = event.target.closest?.('input, textarea, select, [contenteditable=""], [contenteditable=true]');
  if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k') { event.preventDefault(); open(); }
  else if (event.key === '/' && !typing && !event.metaKey && !event.ctrlKey && !event.altKey && !dialog?.open) { event.preventDefault(); open(); }
});
})();"""
HASH = b64encode(sha256(SCRIPT.encode()).digest()).decode()
TAG = '<script>' + SCRIPT + '</script>'

CSS = """
.jump-trigger {
  display: inline-flex; align-items: center; gap: 10px; min-height: 44px; min-width: 44px;
  margin-inline: 0 auto; padding: 0 8px 0 14px; border: 1px solid var(--line); border-radius: 999px;
  background: var(--panel); color: var(--muted); font: 13px/1 var(--sans); cursor: pointer;
}
.jump-trigger:hover { color: var(--fg); border-color: var(--line-strong); }
.jump-trigger svg, .jump svg { flex: none; width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.jump-trigger kbd {
  padding: 4px 7px; border: 1px solid var(--line); border-radius: 999px; background: var(--bg);
  color: var(--muted); font: 11px/1 var(--mono);
}
.page-document .site-header:has(> .jump-trigger) { grid-template-columns: auto auto minmax(0, 1fr) auto; }
.page-document .site-header .jump-trigger { margin-inline: 8px 0; justify-self: start; }
#content > h1:has(+ .post-meta) { width: fit-content; max-width: 100%; }
.jump {
  width: min(600px, calc(100vw - 32px)); max-height: min(560px, calc(100dvh - 120px)); margin: 12vh auto auto;
  padding: 0; border: 1px solid var(--line); border-radius: 20px; background: var(--raised); color: var(--fg);
  box-shadow: var(--shadow-pop); overflow: hidden;
}
.jump[open] { display: flex; flex-direction: column; }
.jump::backdrop { background: color-mix(in srgb, var(--bg) 40%, #0000008c); -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px); }
.jump-field { display: flex; align-items: center; gap: 12px; padding: 0 20px; border-bottom: 1px solid var(--line); color: var(--muted); }
.jump-field svg { width: 18px; height: 18px; }
.jump-field input {
  flex: 1; min-width: 0; min-height: 60px; border: 0; background: transparent; color: var(--fg);
  font: 17px/1.4 var(--sans); outline: none;
}
.jump-field input::placeholder { color: var(--muted); }
.jump-list { overflow-y: auto; padding: 8px; overscroll-behavior: contain; }
.jump-item {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 12px;
  min-height: 46px; padding: 0 14px; border-radius: 12px; color: var(--fg); text-decoration: none;
}
.jump-item svg { color: var(--muted); }
.jump-item[aria-selected=true] { background: var(--accent-wash); }
.jump-item[aria-selected=true] svg { color: var(--accent); }
.jump-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 15px; }
.jump-path { color: var(--muted); font: 12px/1 var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; }
.jump-status { margin: 0; padding: 0 22px; color: var(--muted); font-size: 14px; }
.jump-status:not(:empty) { padding-block: 20px; }
.jump-hint { margin: 0; padding: 10px 20px; border-top: 1px solid var(--line); color: var(--muted); font: 11.5px/1.5 var(--mono); }
@media (max-width: 640px) {
  .jump-trigger { margin-inline-start: auto; padding-inline: 13px; }
  .jump-trigger-label, .jump-trigger kbd { display: none; }
  .page-home .site-header .jump-trigger { order: 0; }
  .jump { margin-top: 10px; }
  .jump-path { display: none; }
  .jump-hint { display: none; }
}
@media (max-width: 380px) {
  .page-document .site-header .jump-trigger { display: none; }
  .page-document .site-header:has(> .jump-trigger) { grid-template-columns: auto minmax(0, 1fr) auto; }
}
@media (prefers-reduced-motion: no-preference) {
  .jump[open] { animation: msg-pop .26s var(--ease-out); }
  .jump-item { transition: background-color .12s ease; }
  @view-transition { navigation: auto; }
  #content > h1:has(+ .post-meta) { view-transition-name: msg-title; }
  ::view-transition-old(root) { animation: msg-page-out .16s ease-in both; }
  ::view-transition-new(root) { animation: msg-page-in .38s var(--ease-out) .06s both; }
  ::view-transition-group(msg-title) { animation-duration: .52s; animation-timing-function: var(--ease-out); }
  ::view-transition-old(msg-title), ::view-transition-new(msg-title) { animation-duration: .52s; height: 100%; object-fit: contain; object-position: left top; }
}
@media (prefers-reduced-motion: no-preference) and (min-width: 641px) {
  .site-header { view-transition-name: msg-header; }
  ::view-transition-group(msg-header) { animation-duration: .3s; }
}
@keyframes msg-page-out { to { opacity: 0; } }
@keyframes msg-page-in { from { opacity: 0; transform: translateY(10px); } }
"""
