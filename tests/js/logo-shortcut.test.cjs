const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '../../src/msg/transports/webmcp.py'), 'utf8',
);
const handler = source.slice(
  source.indexOf("  const brand = document.querySelector('a.brand');"),
  source.indexOf('  const translations ='),
);
assert.ok(handler.length > 0);

function browser() {
  const data = new Map();
  let now = 10000;
  let destination;
  let click;
  const load = () => vm.runInNewContext(handler, {
    document: { querySelector: () => ({ addEventListener: (_, fn) => { click = fn; } }) },
    sessionStorage: {
      getItem: key => data.get(key),
      setItem: (key, value) => data.set(key, value),
      removeItem: key => data.delete(key),
    },
    Date: { now: () => now }, JSON, Number,
    location: { assign: target => { destination = target; } },
  });
  return {
    data, load,
    advance: ms => { now += ms; },
    destination: () => destination,
    tap(overrides = {}) {
      let prevented = false;
      click({ button: 0, preventDefault: () => { prevented = true; }, ...overrides });
      return prevented;
    },
  };
}

test('three quick clicks survive page navigation and open the introduction', () => {
  const page = browser();
  page.load();
  assert.equal(page.tap(), false);
  page.advance(500);
  page.load();
  assert.equal(page.tap(), false);
  page.advance(500);
  page.load();
  assert.equal(page.tap(), true);
  assert.equal(page.destination(), '/@root/web');
  assert.equal(page.data.size, 0);
});

test('slow clicks reset the gesture and modifier clicks do not count', () => {
  const page = browser();
  page.load();
  page.tap();
  page.advance(4000);
  page.load();
  page.tap();
  assert.equal(page.destination(), undefined);
  const before = page.data.get('msg-logo-run');
  for (const modifiers of [{ ctrlKey: true }, { metaKey: true }, { button: 1 }]) {
    assert.equal(page.tap(modifiers), false);
    assert.equal(page.data.get('msg-logo-run'), before);
  }
});
