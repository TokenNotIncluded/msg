const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const script = fs.readFileSync('src/msg/transports/post_actions.py', 'utf8').split('POST_ACTIONS_SCRIPT = r"""')[1].split('"""')[0];

function element() {
  return {dataset:{}, value:'', hidden:true, events:{}, children:[],
    get textContent() {return this.text || '';},
    set textContent(value) {this.text=value;this.children=[];},
    addEventListener(name, callback) {this.events[name] = callback;},
    setAttribute(name, value) {this[name] = value;},
    removeAttribute(name) {delete this[name];},
    append(child) {this.children.push(child);}, focus() {this.focused = true;}};
}

function page(options={}) {
  const kinds = ['ACK','USED','VERIFIED','SOLVED','THANKS','fork','bookmark','follow','comment'];
  const buttons = kinds.map(kind => {
    const button = element(); button.dataset.action = kind;
    const span = element(), small = element();
    button.querySelector = selector => selector === 'span' ? span : ['ACK','USED','VERIFIED','SOLVED','THANKS'].includes(kind) ? small : null;
    return button;
  });
  const form = element(), field = element(), status = element(), label = element(), submit = element(), collection = element(), close = element();
  const panel = element(); panel.dataset = {signedIn:'true',id:'post',author:'author',revision:'displayed-revision',csrf:'csrf'};
  panel.querySelectorAll = () => buttons;
  panel.querySelector = selector => ({form,textarea:field,'[role=status]':status,label,'[type=submit]':submit,'[data-copy=collection]':collection,'[data-copy=proofs]':element(),'[data-copy=forks]':element(),'[data-copy=claims]':element(),'[data-copy=claim-note]':element(),'[data-compose-close]':close})[selector];
  const calls = [], notifications = [];
  vm.runInNewContext(script, {
    document:{documentElement:{lang:'en'},getElementById:()=>panel,createElement:element},
    window:{dispatchEvent:event=>notifications.push(event)},
    CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
    MutationObserver:class {observe() {}},
    crypto:{randomUUID:()=>String(calls.length)}, AbortSignal:{timeout:()=>null},
    location:{assign() {throw Error('Unexpected login');}},
    fetch: async (url, args) => {
      calls.push({url,args});
      if(url.startsWith('/_post/state')) {
        if(options.state) return options.state();
        if(options.stateError) return {ok:false,json:async()=>({status:'error',error:{code:options.stateError}})};
        return {ok:true,json:async()=>({status:'ok',data:{proofs:{USED:1},my_proofs:options.publicFallback?[]:['USED'],personal_state_available:!options.publicFallback}})};
      }
      if(options.write) return options.write(url,args);
      return {ok:true,json:async()=>({status:'ok',data:{proofs:{USED:1},my_proofs:['USED']},resources:[{id:'branch'}]})};
    }
  });
  return {buttons,form,field,status,calls,submit,close,notifications};
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

test('state read failure keeps independent composers usable and counts unknown without writes', async () => {
  const ui=page({stateError:'credential_ceiling'}); await new Promise(resolve=>setImmediate(resolve));
  for(const kind of ['comment','fork','ACK','USED','VERIFIED','SOLVED','THANKS']) {
    const button=ui.buttons.find(item=>item.dataset.action===kind);
    assert.equal(button.disabled,false);
    if(kind==='USED') assert.equal(button.querySelector('small').textContent,'—');
  }
  for(const kind of ['bookmark','follow']) assert.equal(ui.buttons.find(item=>item.dataset.action===kind).disabled,true);
  assert.match(ui.status.textContent,/authorization does not include/);
  assert.equal(ui.status.children.length,0);
  await ui.buttons.find(item=>item.dataset.action==='fork').events.click();
  assert.equal(ui.form.hidden,false); assert.equal(ui.calls.length,1);
});

test('public fallback displays public counts without inventing personal toggle state', async () => {
  const ui=page({publicFallback:true}); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(ui.buttons.find(item=>item.dataset.action==='USED').querySelector('small').textContent,'1');
  for(const kind of ['bookmark','follow','USED']) assert.equal(ui.buttons.find(item=>item.dataset.action===kind)['aria-pressed'],undefined);
  for(const kind of ['bookmark','follow']) assert.equal(ui.buttons.find(item=>item.dataset.action===kind).disabled,true);
  assert.match(ui.status.textContent,/Showing public proof counts/);
  assert.equal(ui.calls.length,1);
});

test('a late initial read cannot replace an explicit claim with stale or anonymous proof state', async () => {
  for(const publicFallback of [false,true]) {
    let finishRead;
    const ui=page({state:()=>new Promise(resolve=>{finishRead=resolve;})});
    await ui.buttons.find(item=>item.dataset.action==='USED').events.click();
    ui.field.value='Fresh claim';
    await ui.form.events.submit({preventDefault(){}});
    const proof=ui.buttons.find(item=>item.dataset.action==='USED');
    assert.equal(proof.querySelector('small').textContent,'1');
    assert.equal(proof['aria-pressed'],'true');
    assert.equal(ui.buttons.find(item=>item.dataset.action==='bookmark').disabled,true);
    finishRead({ok:true,json:async()=>({status:'ok',data:{proofs:{USED:0},my_proofs:[],personal_state_available:!publicFallback}})});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(proof.querySelector('small').textContent,'1');
    assert.equal(proof['aria-pressed'],'true');
    assert.equal(ui.buttons.find(item=>item.dataset.action==='bookmark').disabled,publicFallback);
    assert.match(ui.status.textContent,/Claim recorded/);
    assert.equal(ui.calls.length,2);
  }
});

test('authorization failure retains draft and request ID; only expired sign-in offers login', async () => {
  for(const code of ['credential_ceiling','invalid_grant']) {
    const ui=page({write:async()=>({ok:false,json:async()=>({status:'error',error:{code}})})});
    await new Promise(resolve=>setImmediate(resolve));
    await ui.buttons.find(item=>item.dataset.action==='USED').events.click();
    ui.field.value='Evidence draft';
    await ui.form.events.submit({preventDefault(){}});
    assert.equal(ui.field.value,'Evidence draft'); assert.equal(ui.form.hidden,false);
    assert.equal(ui.field.disabled,false); assert.equal(ui.submit.disabled,false);
    assert.equal(ui.status.children.length,code==='invalid_grant'?1:0);
    if(code==='invalid_grant') assert.equal(ui.status.children[0].href,'/login');
    await ui.form.events.submit({preventDefault(){}});
    const first=JSON.parse(ui.calls[1].args.body), second=JSON.parse(ui.calls[2].args.body);
    assert.equal(first.request_id,second.request_id); assert.equal(first.revision,'displayed-revision');
    assert.equal(first.note,'Evidence draft'); assert.equal(first.kind,'USED');
  }
});

test('pending write locks composer switching and preserves the submitted operation', async () => {
  let finish;
  const ui=page({write:()=>new Promise(resolve=>{finish=resolve;})});
  await new Promise(resolve=>setImmediate(resolve));
  await ui.buttons.find(item=>item.dataset.action==='comment').events.click();
  ui.field.value='Comment draft';
  const sending=ui.form.events.submit({preventDefault(){}});
  assert.equal(ui.field.disabled,true); assert.equal(ui.close.disabled,true);
  assert.ok(ui.buttons.every(button=>button.disabled));
  await ui.buttons.find(item=>item.dataset.action==='fork').events.click();
  assert.equal(ui.field.required,true);
  assert.equal(ui.calls.length,2);
  finish({ok:true,json:async()=>({status:'ok',data:{},resources:[{id:'reply'}]})});
  await sending;
  assert.equal(JSON.parse(ui.calls[1].args.body).operation,'discussion.reply');
  assert.match(ui.status.textContent,/Comment posted/);
  assert.equal(ui.status.children[0].textContent,'View comment');
  assert.equal(ui.notifications.length,1);
  assert.equal(ui.notifications[0].type,'msg:reply-posted');
  assert.equal(ui.notifications[0].detail.parent,'post');
  assert.equal(ui.field.disabled,false); assert.equal(ui.close.disabled,false);
});

test('closing the composer retains its draft, and following targets the author', async () => {
  const ui=page(); await new Promise(resolve=>setImmediate(resolve));
  await ui.buttons.find(item=>item.dataset.action==='comment').events.click();
  ui.field.value='Still here'; ui.close.events.click(); assert.equal(ui.form.hidden,true);
  await ui.buttons.find(item=>item.dataset.action==='comment').events.click(); assert.equal(ui.field.value,'Still here');
  await ui.buttons.find(item=>item.dataset.action==='follow').events.click();
  const packet=JSON.parse(ui.calls[1].args.body);
  assert.equal(packet.operation,'communication.follow'); assert.equal(packet.id,'author');
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
