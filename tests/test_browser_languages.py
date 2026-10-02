"""Language choice survives navigation and preserves unknown content and RTL code."""

import json
import re
import subprocess

from msg.transports.browser_i18n import ACTION_LABELS, LANGUAGES, TRANSLATIONS
from msg.transports.browser_style import PREFERENCES
from msg.transports.webmcp import WEBMCP_SCRIPT


def test_locale_catalog_and_native_picker_labels():
    assert {code for code, *_ in LANGUAGES} == {'en', 'zh', 'hi', 'es', 'ar', 'fr', 'bn', 'pt'}
    for code, label, tag, _ in LANGUAGES:
        assert f'<option value="{code}" lang="{tag}">{label}</option>' in PREFERENCES
    base_keys = set(re.findall(r'\b(\w+):\s*\[', WEBMCP_SCRIPT.split('const locales')[0]))
    assert base_keys | set(ACTION_LABELS) <= set(TRANSLATIONS['es'])
    assert len({frozenset(messages) for messages in TRANSLATIONS.values()}) == 1


def test_browser_language_switching_fallback_and_persistence():
    program = r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
const script = require('node:fs').readFileSync(0, 'utf8');
function page(saved, browserLanguages) {
  const storage = new Map(saved ? [['msg.language', saved]] : []);
  const home = {dataset: {i18n: 'home'}, textContent: 'Home'};
  const unknown = {dataset: {i18n: 'not_a_catalog_key'}, textContent: 'User content'};
  const search = {dataset: {i18nPlaceholder: 'search_query'}};
  const selector = {addEventListener(name, callback) {this.change = callback;}};
  const root = {clientWidth: 1080, dataset: {}, style: {setProperty() {}}};
  const document = {
    documentElement: root,
    querySelector() {return null;},
    querySelectorAll(selector) {
      if (selector === '[data-i18n]') return [home, unknown];
      if (selector === '[data-i18n-placeholder]') return [search];
      return [];
    },
    getElementById(id) {return id === 'msg-language' ? selector : null;},
    addEventListener() {},
  };
  const context = {document, navigator: {languages: browserLanguages},
    window: {addEventListener() {}}, addEventListener() {},
    requestAnimationFrame() {return 1;},
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}};
  vm.runInNewContext(script, context);
  return {root, home, unknown, search, selector, storage, context};
}
const initial = page(undefined, ['xx', 'es-MX']);
assert.equal(initial.home.textContent, 'Inicio');
assert.equal(initial.search.placeholder, 'Buscar en MSG');
assert.equal(initial.unknown.textContent, 'User content');
const expected = {en: 'Home', zh: '首页', hi: 'मुखपृष्ठ', es: 'Inicio', ar: 'الرئيسية', fr: 'Accueil', bn: 'মূল পাতা', pt: 'Início'};
for (const [code, label] of Object.entries(expected)) {
  initial.selector.change({target: {value: code}});
  assert.equal(initial.home.textContent, label);
  assert.equal(initial.root.dir, code === 'ar' ? 'rtl' : 'ltr');
  assert.equal(initial.root.lang, code === 'zh' ? 'zh-CN' : code);
  assert.equal(initial.storage.get('msg.language'), code);
  const next = page(code, ['en']);
  assert.equal(next.home.textContent, label);
  assert.equal(next.selector.value, code);
}
initial.selector.change({target: {value: 'ar-EG'}});
assert.equal(initial.context.msgText('Comment', '评论'), 'تعليق');
initial.selector.change({target: {value: 'zh'}});
assert.equal(initial.context.msgText('Following', '已关注'), '已关注');
initial.selector.change({target: {value: 'unsupported'}});
assert.equal(initial.root.lang, 'en');
assert.equal(initial.root.dir, 'ltr');
assert.equal(initial.home.textContent, 'Home');
assert.equal(page('bn', ['en']).home.textContent, 'মূল পাতা');
process.stdout.write(JSON.stringify({ok: true}));
"""
    result = subprocess.run(
        ['node', '-e', program],
        input=WEBMCP_SCRIPT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'ok': True}
