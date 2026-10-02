const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const html = fs.readFileSync('src/msg/data/public-terminal.html', 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];

function page(options = {}) {
  const calls = [], timers = [];
  const document = {activeElement:null};
  function element(tagName = 'div') {
    const node = {tagName, children:[], events:{}, value:'', className:'', readOnly:false,
      get textContent() {return this.text || '';},
      set textContent(value) {this.text = value; this.children = [];},
      set innerHTML(_) {throw Error('HTML insertion is forbidden');},
      get firstElementChild() {return this.children[0];},
      append(child) {child.parent = this; this.children.push(child);},
      remove() {this.parent.children.splice(this.parent.children.indexOf(this), 1);},
      replaceChildren() {this.children = [];},
      setAttribute(name, value) {this[name] = value;},
      addEventListener(name, callback) {this.events[name] = callback;},
      focus() {document.activeElement = this;}, scrollIntoView() {}};
    return node;
  }
  const transcript = element(), form = element('form'), input = element('input');
  const run = element('button'), status = element();
  const nodes = {'#transcript':transcript, '#prompt':form, '#command':input, '#run':run, '#status':status};
  document.querySelector = selector => nodes[selector];
  document.createElement = element;
  document.activeElement = input;
  const submit = () => form.events.submit({preventDefault(){}});
  form.requestSubmit = submit;
  vm.runInNewContext(script, {document, URL, location:{origin:'https://msg.test'}, AbortController,
    setTimeout(callback) {const timer = {callback}; timers.push(timer); return timer;},
    clearTimeout(timer) {timer.cleared = true;},
    fetch(url, args) {
      calls.push({url, args});
      return options.fetch ? options.fetch(url, args) : Promise.resolve({ok:true, status:200,
        json:async () => options.data || {output:'ready'}});
    }});
  return {transcript, form, input, run, status, calls, timers, submit,
    text:() => transcript.children.filter(node => node.tagName === 'pre').map(node => node.textContent).join('\n'),
    key(key, extras = {}) {
      const event = {key, prevented:false, preventDefault() {this.prevented = true;}, ...extras};
      input.events.keydown(event); form.events.keydown(event); return event;
    }};
}

test('parameter reads send one anonymous GET query and keep multiword Unicode searches', async () => {
  const ui = page();
  for (const command of ['topics', 'help read', 'ls /main', 'read r_example', 'search 可读 内容', 'users', 'user @reader', 'rules']) {
    ui.input.value = command; await ui.submit();
    const request = ui.calls.at(-1);
    assert.equal(request.url, '/_terminal?command=' + encodeURIComponent(command));
    assert.equal(request.args.credentials, 'omit');
    assert.equal(request.args.cache, 'no-store');
    assert.equal(request.args.method, undefined);
    assert.equal(request.args.headers, undefined);
    assert.equal(ui.input.value, '');
  }
  assert.equal(ui.calls.length, 8);
});

test('unknown commands, missing arguments, extra arguments and oversized input show usage without requests', async () => {
  const ui = page();
  for (const command of ['ls', 'read', 'search', 'user', 'status extra', 'clear extra', 'help;status', 'search a\nserver', 'clear\n', 'search ' + 'a'.repeat(81), 'search a b c d e f g h i', 'search ' + 'a'.repeat(512)]) {
    ui.input.value = command; await ui.submit();
    assert.equal(ui.input.value, command);
  }
  assert.equal(ui.calls.length, 0);
  assert.match(ui.text(), /Use: ls \/main/);
  assert.match(ui.text(), /Unknown command/);
  assert.ok(html.includes('maxlength="512"'));
});

test('Tab completes commands, gives multiple matches, and learns path and handle arguments from public results', async () => {
  const ui = page({data:{output:'Public records', links:[
    {href:'/main/one.md', label:'One'}, {href:'/main/other.md', label:'Other'}, {href:'/@reader', label:'Reader'}]}});
  ui.input.value = 'st'; assert.equal(ui.key('Tab').prevented, true);
  assert.equal(ui.input.value, 'stat'); assert.match(ui.status.textContent, /stats, status/);
  ui.input.value = 'rea'; ui.key('Tab'); assert.equal(ui.input.value, 'read');
  ui.input.value = 'feed'; assert.equal(ui.key('Tab').prevented, false);
  ui.input.value = 'topics'; await ui.submit();
  ui.input.value = 'read /main/o'; ui.key('Tab');
  assert.equal(ui.input.value, 'read /main/o'); assert.match(ui.status.textContent, /one.md.*other.md/);
  ui.input.value = 'read /main/on'; ui.key('Tab'); assert.equal(ui.input.value, 'read /main/one.md');
  ui.input.value = 'user re'; ui.key('Tab'); assert.equal(ui.input.value, 'user reader');
  ui.input.value = 'user @re'; ui.key('Tab'); assert.equal(ui.input.value, 'user @reader');
  ui.input.value = 'search rea'; assert.equal(ui.key('Tab').prevented, false);
  assert.equal(ui.calls.length, 1);
});

test('results stay literal text and only safe same-origin resource paths become links', async () => {
  const ui = page({data:{output:'<script>window.bad=1</script>', links:[
    {href:'/main/one.md', label:'<img src=x onerror=bad()>'},
    {href:'https://elsewhere.test/main', label:'external'}, {href:'//elsewhere.test/main'},
    {href:'javascript:alert(1)'}, {href:'/\\elsewhere.test/main'}, {href:'/main?token=secret'},
    {href:'/main#fragment'}, {href:'/-/p/content.post_create'}, {href:'/_terminal'},
    {href:'/oauth/approve'}, {href:'/login'}, {href:'/logout'},
    {href:'/main/中文.md', label:'中文'},
    {href:'/main/two', command:'content.post_create /main'}]}});
  ui.input.value = 'feed'; await ui.submit();
  assert.match(ui.text(), /<script>window.bad=1<\/script>/);
  const row = ui.transcript.children.find(node => node.className === 'result-links');
  const links = row.children.filter(node => node.tagName === 'a');
  assert.deepEqual(links.map(link => link.href), ['/main/one.md', '/main/中文.md', '/main/two']);
  assert.equal(links[0].textContent, '<img src=x onerror=bad()>');
  const buttons = row.children.filter(node => node.tagName === 'button');
  assert.equal(buttons.length, 2);
  await buttons[0].events.click();
  assert.equal(ui.calls.at(-1).url, '/_terminal?command=read%20%2Fmain%2Fone.md');
});

test('encoded reserved routes, separators and controls are rejected while Unicode resource paths round-trip', async () => {
  const ui = page({data:{output:'Unicode resource', links:[
    {href:'/%6cogin'}, {href:'/%5fterminal'}, {href:'/%2d/p/file.create'},
    {href:'/main/a%0ab.md'}, {href:'/main/%5cother.md'}, {href:'/%252d/p/file.create'},
    {href:'/main/%252e%252e/login'}, {href:'/main/../login'}, {href:'/main/%zz.md'},
    {href:'/main/%E4%B8%AD%E6%96%87.md', label:'中文'}]}});
  ui.input.value = 'feed'; await ui.submit();
  const row = ui.transcript.children.find(node => node.className === 'result-links');
  const links = row.children.filter(node => node.tagName === 'a');
  assert.deepEqual(links.map(link => link.href), ['/main/中文.md']);
  await row.children.find(node => node.tagName === 'button').events.click();
  assert.equal(ui.calls.at(-1).url, '/_terminal?command=' + encodeURIComponent('read /main/中文.md'));
});

test('private and invalid results explain the read failure and retain the input', async () => {
  for (const [status, code, expected] of [[403,'permission_denied',/not publicly readable/],
    [404,'not_found',/No public resource/], [400,'invalid_command',/Use: read/]]) {
    const ui = page({fetch:async () => ({ok:false, status, json:async () => ({error:{code}})})});
    ui.input.value = 'read /main/private.md'; await ui.submit();
    assert.match(ui.text(), expected); assert.equal(ui.input.value, 'read /main/private.md');
    assert.equal(ui.input.readOnly, false); assert.equal(ui.form['aria-busy'], 'false');
  }
});

test('history preserves drafts, IME blocks submission, and Escape aborts only the active read', async () => {
  const ui = page();
  ui.input.value = 'topics'; await ui.submit();
  ui.input.value = 'search draft'; ui.key('ArrowUp'); assert.equal(ui.input.value, 'topics');
  ui.key('ArrowDown'); assert.equal(ui.input.value, 'search draft');
  ui.input.events.compositionstart(); await ui.submit(); assert.equal(ui.calls.length, 1);
  assert.equal(ui.key('Tab').prevented, false); ui.input.events.compositionend();
  const waiting = page({fetch:(_, args) => new Promise((_, reject) => {
    args.signal.addEventListener('abort', () => reject(Error('aborted')));
  })});
  waiting.input.value = 'read /main/one.md'; const running = waiting.submit();
  assert.equal(waiting.input.readOnly, true); assert.equal(waiting.run.textContent, 'Cancel');
  assert.equal(waiting.key('Escape').prevented, true); await running;
  assert.match(waiting.text(), /Command cancelled/); assert.equal(waiting.input.value, 'read /main/one.md');
  assert.equal(waiting.input.readOnly, false); assert.equal(waiting.timers[0].cleared, true);
});

test('timeout ignores a late successful response and restores the original command', async () => {
  let finish;
  const ui = page({fetch:() => new Promise(resolve => {finish = resolve;})});
  ui.input.value = 'read /main/one.md'; const running = ui.submit();
  ui.timers[0].callback();
  assert.equal(ui.calls[0].args.signal.aborted, true);
  finish({ok:true, status:200, json:async () => ({output:'LATE_SECRET', links:[]})});
  await running;
  assert.match(ui.text(), /did not respond within 15 seconds/);
  assert.ok(!ui.text().includes('LATE_SECRET'));
  assert.equal(ui.input.value, 'read /main/one.md');
  assert.equal(ui.input.readOnly, false); assert.equal(ui.run.textContent, 'Run');
  assert.equal(ui.form['aria-busy'], 'false');
});

test('transcript, command history and rendered links stay bounded', async () => {
  const ui = page({data:{output:'ready', links:Array.from({length:50}, (_, i) => ({href:'/main/p' + i + '.md'}))}});
  for (let i = 0; i < 110; i++) {ui.input.value = 'search item' + i; await ui.submit();}
  assert.ok(ui.transcript.children.length <= 160);
  const rows = ui.transcript.children.filter(node => node.className === 'result-links');
  assert.ok(rows.every(row => row.children.filter(node => node.tagName === 'a').length <= 20));
  assert.ok(rows.every(row => row.children.length <= 40));
  for (let i = 0; i < 101; i++) ui.key('ArrowUp');
  assert.equal(ui.input.value, 'search item10');
  ui.input.value = 'clear'; await ui.submit(); assert.equal(ui.transcript.children.length, 0);
  assert.equal(ui.calls.length, 110);
});
