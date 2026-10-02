"""Compact navigation preserves links and behaves like a dismissible disclosure."""

import json
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from msg.transports.home_page import account_navigation, document_html, home_html
from msg.transports.webmcp import WEBMCP_SCRIPT


class Navigation(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.summaries = []
        self.details = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == 'a':
            self.links.append(attributes)
        elif tag == 'summary':
            self.summaries.append(attributes)
        elif tag == 'details':
            self.details.append(attributes)


def test_compact_anonymous_navigation_keeps_both_login_routes():
    navigation = Navigation(account_navigation(None, compact=True))
    assert [link['href'] for link in navigation.links] == ['/login', '/register']
    assert navigation.details == [{'class': 'account-menu'}]
    assert 'role' not in navigation.summaries[0]
    assert 'data-i18n="account_menu"' in account_navigation(None, compact=True)
    assert '<details class="account-menu">' in document_html('# Test').decode()


def test_compact_account_preserves_routes_and_escapes_full_name():
    name = '@long"<account>'
    navigation = Navigation(account_navigation({'name': name}, compact=True))
    assert navigation.summaries[0]['title'] == name
    assert len(navigation.links) == 8
    assert navigation.links[0]['href'] == '/@long%22%3Caccount%3E'
    assert navigation.links[-2] == {'href': '/bookmarks', 'data-i18n': 'saved'}
    assert navigation.links[-1]['href'] == '/oauth/logout'
    home = home_html(account={'name': name}).decode().split('</header>', 1)[0]
    assert home.count('<details class="account-menu">') == 1
    assert '<account>' not in home


def test_translatable_channel_description_preserves_permission_help_link():
    home = home_html({
        'posts': 0,
        'posts_today': 0,
        'users': 0,
        'date': '2026-10-02',
        'timezone': 'Asia/Taipei',
        'latest': [],
        'channels': [
            {'name': 'main', 'path': '/main', 'about': '', 'posts': 0, 'mode': 'rw', 'posting': ''}
        ],
    }).decode()
    description = home.split('data-i18n="channel_hint">', 1)[1].split('</span>', 1)[0]
    assert '/help/permissions' not in description
    assert any(
        link.get('href') == '/help/permissions' and link.get('data-i18n') == 'permission_bits'
        for link in Navigation(home).links
    )


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is needed for DOM interaction probe')
def test_menu_keyboard_dismissal_and_short_viewport_positioning():
    # Execute the shipped adapter against a minimal DOM rather than matching its source.
    program = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = fs.readFileSync(0, 'utf8');
const listeners = {};
const frames = [];
const document = {
  documentElement: {clientWidth: 320, lang: 'en', dataset: {}, style: {setProperty() {}}},
  querySelector() { return null; }, getElementById() { return null; },
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); }
};
const makePanel = kind => {
  const properties = {};
  const summary = {
    getBoundingClientRect() { return {right: 304, top: 20, bottom: 64}; },
    focus() { document.activeElement = summary; }
  };
  const link = {};
  const fields = {scrollHeight: 500, style: {setProperty(k, v) { properties[k] = v; }}};
  const panel = {
    open: false, summary, link, fields, properties, events: {},
    classList: {contains(name) { return name === kind; }},
    querySelector(selector) { return selector === 'summary' ? summary : fields; },
    contains(node) { return node === summary || node === link || node === fields; },
    addEventListener(name, fn) { this.events[name] = fn; }
  };
  return panel;
};
const account = makePanel('account-menu');
const preferences = makePanel('preferences');
const label = {dataset: {i18n: 'account_menu'}, textContent: 'Account'};
document.querySelectorAll = selector => {
  if (selector === '.preferences, .account-menu') return [account, preferences];
  if (selector === '[data-i18n]') return [label];
  return [];
};
const viewport = {width: 320, height: 180, offsetLeft: 0, offsetTop: 0, addEventListener() {}};
const storage = {getItem() {return 'zh';}, setItem() {}};
vm.runInNewContext(script, {
  document, window: {innerHeight: 180, visualViewport: viewport, addEventListener() {}},
  localStorage: storage, navigator: {},
  requestAnimationFrame(fn) {frames.push(fn); return frames.length;},
});
assert.equal(label.textContent, '账号');
const dispatch = (name, event) => listeners[name].forEach(fn => fn(event));
account.open = true; account.events.toggle();
assert.equal(account.properties['--menu-width'], '280px');
assert.equal(account.properties['--menu-left'], '24px');
assert.equal(account.properties['--menu-top'], '16px');
assert.equal(account.properties['--menu-height'], '148px');
document.activeElement = account.link;
dispatch('focusin', {target: account.link});
assert.equal(account.open, true);
dispatch('keydown', {key: 'Escape'});
assert.equal(account.open, false);
assert.equal(document.activeElement, account.summary);
account.open = true; account.events.toggle();
const outside = {};
document.activeElement = outside;
dispatch('focusin', {target: outside});
assert.equal(account.open, false);
assert.equal(document.activeElement, outside);
account.open = true; account.events.toggle();
preferences.open = true; preferences.events.toggle();
assert.equal(account.open, false);
assert.equal(preferences.open, true);
assert.equal(preferences.properties['--menu-width'], '288px');
dispatch('click', {target: outside});
assert.equal(preferences.open, false);
process.stdout.write(JSON.stringify({ok: true}));
"""
    result = subprocess.run(
        ['node', '-e', program],
        input=WEBMCP_SCRIPT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == {'ok': True}
