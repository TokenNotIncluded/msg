const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const script = fs.readFileSync('src/msg/transports/post_actions.py', 'utf8').split('POST_ACTIONS_SCRIPT = r"""')[1].split('"""')[0];

function element() {
  return {dataset:{}, value:'', hidden:true, events:{}, children:[],
    addEventListener(name, callback) {this.events[name] = callback;},
    setAttribute(name, value) {this[name] = value;},
    append(child) {this.children.push(child);}, focus() {this.focused = true;}};
}

function page() {
  const kinds = ['ACK','USED','VERIFIED','SOLVED','THANKS','fork','bookmark','comment'];
  const buttons = kinds.map(kind => {
    const button = element(); button.dataset.action = kind;
    const span = element(), small = element();
    button.querySelector = selector => selector === 'span' ? span : ['ACK','USED','VERIFIED','SOLVED','THANKS'].includes(kind) ? small : null;
    return button;
  });
  const form = element(), field = element(), status = element(), label = element(), submit = element(), collection = element();
  const panel = element(); panel.dataset = {signedIn:'true',id:'post',revision:'displayed-revision',csrf:'csrf'};
  panel.querySelectorAll = () => buttons;
  panel.querySelector = selector => ({form,textarea:field,'[role=status]':status,label,'[type=submit]':submit,'[data-copy=collection]':collection,'[data-copy=proofs]':element(),'[data-copy=forks]':element()})[selector];
  const calls = [];
  vm.runInNewContext(script, {
    document:{documentElement:{lang:'en'},getElementById:()=>panel,createElement:element},
    MutationObserver:class {observe() {}},
    crypto:{randomUUID:()=>String(calls.length)}, AbortSignal:{timeout:()=>null},
    location:{assign() {throw Error('Unexpected login');}},
    fetch: async (url, args) => {
      calls.push({url,args});
      return {ok:true,json:async()=>({status:'ok',data:{proofs:{USED:1},my_proofs:['USED']},resources:[{id:'branch'}]})};
    }
  });
  return {buttons,form,field,status,calls};
}

test('reading never writes; a claim needs explicit submit and pins displayed revision', async () => {
  const ui = page(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(ui.calls.length,1); assert.ok(ui.calls[0].url.includes('revision=displayed-revision'));
  await ui.buttons.find(button=>button.dataset.action==='USED').events.click();
  assert.equal(ui.calls.length,1); assert.equal(ui.field.required,false);
  ui.field.value = 'Used successfully';
  await ui.form.events.submit({preventDefault(){}});
  const packet = JSON.parse(ui.calls[1].args.body);
  assert.equal(packet.operation,'discussion.prove'); assert.equal(packet.kind,'USED');
  assert.equal(packet.revision,'displayed-revision'); assert.equal(packet.note,'Used successfully');
  assert.equal(ui.form.hidden,true);
});

test('fork requires content and creates a new branch against the displayed revision', async () => {
  const ui = page(); await new Promise(resolve=>setImmediate(resolve));
  await ui.buttons.find(button=>button.dataset.action==='fork').events.click();
  assert.equal(ui.field.required,true);
  await ui.form.events.submit({preventDefault(){}}); assert.equal(ui.calls.length,1);
  ui.field.value='Different solution'; await ui.form.events.submit({preventDefault(){}});
  const packet=JSON.parse(ui.calls[1].args.body);
  assert.equal(packet.operation,'discussion.fork'); assert.equal(packet.revision,'displayed-revision');
  assert.equal(ui.status.children[0].href,'/_id/branch');
});
