"""Execute terminal keyboard and interrupted-request behavior without network access."""

import shutil
import subprocess
from importlib.resources import files

import pytest

NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(
    NODE is None, reason='Node is required to execute browser JavaScript'
)

HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = new Map();
let document;
class Element {
  constructor() {
    this.listeners = new Map(); this.children = []; this.attributes = new Map();
    this.value = ''; this.textContent = ''; this.disabled = false; this.readOnly = false;
  }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  dispatch(name, properties = {}) {
    const event = {prevented:false, preventDefault() {this.prevented = true;}, ...properties};
    const results = (this.listeners.get(name) || []).map(callback => callback(event));
    return {event, done:Promise.all(results)};
  }
  append(child) {child.parent = this; this.children.push(child);}
  replaceChildren() {this.children = [];}
  get firstElementChild() {return this.children[0];}
  remove() {this.parent.children.shift();}
  setAttribute(name, value) {this.attributes.set(name, value);}
  focus() {document.activeElement = this;}
  scrollIntoView() {this.scrolled = true;}
}
for (const name of ['transcript', 'prompt', 'command', 'run', 'status', 'home']) elements.set(name, new Element());
document = {
  querySelector(selector) {return elements.get(selector.slice(1));},
  createElement() {return new Element();}, activeElement:elements.get('command')
};
const timers = new Map();
const calls = [];
const context = {
  document, AbortController,
  setTimeout(callback, duration) {const id = timers.size + 1; timers.set(id, {callback, duration}); return id;},
  clearTimeout(id) {timers.delete(id);},
  fetch(url, options) {
    return new Promise((resolve, reject) => {
      calls.push({url, options, resolve, reject});
      options.signal.addEventListener('abort', () => reject(new Error('aborted')), {once:true});
    });
  }
};
vm.runInNewContext(process.argv[1], context);
const input = elements.get('command'), form = elements.get('prompt');
const run = elements.get('run'), status = elements.get('status'), transcript = elements.get('transcript');
const output = () => transcript.children.map(line => line.textContent).join('\n');
function key(key, properties = {}) {
  const dispatched = input.dispatch('keydown', {key, ...properties});
  for (const callback of form.listeners.get('keydown') || []) callback(dispatched.event);
  return dispatched.event;
}
function submit(command) {if (command !== undefined) input.value = command; return form.dispatch('submit').done;}
function respond(output = 'ready', overrides = {}) {
  calls.at(-1).resolve({ok:true, json:async () => ({output}), ...overrides});
}
async function complete(command, output = command) {const done = submit(command); respond(output); await done;}
const cases = {
  async keyboard() {
    input.value = 'he';
    assert.equal(key('Tab').prevented, true); assert.equal(input.value, 'help');
    assert.equal(key('Tab').prevented, false, 'A completed command must allow Tab to move focus');
    input.value = 'he';
    assert.equal(key('Tab', {shiftKey:true}).prevented, false);
    assert.equal(key('Tab', {ctrlKey:true}).prevented, false);
    assert.equal(key('Tab', {isComposing:true}).prevented, false);
    assert.equal(input.value, 'he');
  },
  async cancel() {
    const done = submit('status');
    assert.equal(input.disabled, false); assert.equal(input.readOnly, true);
    assert.equal(run.disabled, false); assert.equal(run.textContent, 'Cancel');
    assert.match(status.textContent, /Escape/);
    assert.equal(key('Escape').prevented, true);
    await done;
    assert.equal(calls[0].options.signal.aborted, true);
    assert.equal(input.readOnly, false); assert.equal(run.textContent, 'Run');
    assert.equal(input.value, 'status'); assert.match(output(), /Command cancelled/);
    assert.equal(timers.size, 0);
    const retry = submit(); respond('ready'); await retry;
    assert.equal(input.value, ''); assert.match(output(), /ready/);
    assert.equal(calls[1].url, '/_terminal?command=status');
    assert.equal(calls[1].options.credentials, 'omit');
  },
  async cancel_button() {
    const done = submit('feed');
    await submit(); await done;
    assert.equal(calls.length, 1, 'Cancel must not issue a second request');
    assert.match(output(), /Command cancelled/);
    assert.equal(input.value, 'feed');
  },
  async timeout() {
    const done = submit('stats');
    const timer = [...timers.values()][0]; assert.equal(timer.duration, 15000);
    timer.callback(); await done;
    assert.match(output(), /did not respond within 15 seconds/);
    assert.equal(input.value, 'stats'); assert.equal(input.readOnly, false);
    assert.equal(form.attributes.get('aria-busy'), 'false');
    assert.equal(timers.size, 0);
  },
  async focus() {
    const done = submit('status'); elements.get('home').focus(); respond(); await done;
    assert.equal(document.activeElement, elements.get('home'));
    assert.equal(form.scrolled, undefined);
    const next = submit('time'); run.focus(); respond('UTC'); await next;
    assert.equal(document.activeElement, input);
    assert.equal(form.scrolled, true);
  },
  async failure() {
    const done = submit('feed'); respond('', {ok:false}); await done;
    assert.equal(input.value, 'feed'); assert.match(output(), /Could not read the server/);
    assert.equal(input.readOnly, false); assert.equal(timers.size, 0);
    const retry = submit(); respond('<script>alert(1)</script>'); await retry;
    assert.equal(transcript.children.at(-1).textContent, '<script>alert(1)</script>');
  },
  async late_response() {
    let finish;
    const done = submit('feed');
    respond('', {json:() => new Promise(resolve => {finish = resolve;})});
    await new Promise(setImmediate);
    key('Escape'); finish({output:'LATE OUTPUT'}); await done;
    assert.match(output(), /Command cancelled/);
    assert.equal(output().includes('LATE OUTPUT'), false);
  },
  async history() {
    await complete('status'); await complete('time'); input.value = 'draft';
    key('ArrowUp'); assert.equal(input.value, 'time');
    key('ArrowUp'); assert.equal(input.value, 'status');
    key('ArrowUp'); assert.equal(input.value, 'status');
    key('ArrowDown'); key('ArrowDown'); assert.equal(input.value, 'draft');
    key('ArrowUp', {isComposing:true}); assert.equal(input.value, 'draft');
    await submit('clear'); assert.equal(transcript.children.length, 0);
    assert.equal(status.textContent, 'Screen cleared.');
  },
  async composition() {
    input.dispatch('compositionstart'); await submit('status'); assert.equal(calls.length, 0);
    input.dispatch('compositionend'); await complete('status'); assert.equal(calls.length, 1);
  }
};
(async () => {await cases[process.argv[2]]();})().catch(error => {console.error(error); process.exitCode = 1;});
"""


@pytest.mark.parametrize(
    'case',
    [
        'keyboard',
        'cancel',
        'cancel_button',
        'timeout',
        'focus',
        'failure',
        'late_response',
        'history',
        'composition',
    ],
)
def test_terminal_interaction(case):
    html = files('msg.data').joinpath('public-terminal.html').read_text()
    script = html.split('<script>', 1)[1].split('</script>', 1)[0]
    result = subprocess.run(
        [NODE, '-e', HARNESS, script, case], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
