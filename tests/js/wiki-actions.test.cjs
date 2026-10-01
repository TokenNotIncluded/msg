const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const script = fs.readFileSync('src/msg/transports/wiki_actions.py', 'utf8').split('WIKI_SCRIPT = r"""')[1].split('"""')[0];

function element() {
  return {dataset:{}, value:'', hidden:true, events:{}, children:[],
    addEventListener(name, callback) { this.events[name]=callback; },
    replaceChildren(...children) { this.children=children; }, focus() { this.focused=true; }};
}
function page(kind='post') {
  const panel=element(), form=element(), field=element(), title=element(), status=element(), submit=element(), open=element();
  panel.dataset={signedIn:'true',csrf:'csrf',id:'article',revision:'observed',generation:'7',kind};
  form.querySelector=selector=>({'textarea':field,'input':kind==='topic'?title:null,'[type=submit]':submit})[selector];
  panel.querySelector=selector=>({'form':form,'[role=status]':status,'[data-open]':open})[selector];
  const calls=[], answers=[];
  vm.runInNewContext(script, {
    document:{documentElement:{lang:'en'},getElementById:()=>panel,createElement:element},
    crypto:{randomUUID:()=>String(calls.length)},AbortSignal:{timeout:()=>null},
    location:{assign() {throw Error('Unexpected redirect');}},
    fetch:async(url,args)=>{ calls.push({url,args}); const answer=answers.shift() || {status:'ok',data:{generation:8},resources:[{id:'article',revision:'new'}]};
      return {ok:answer.status==='ok',json:async()=>answer}; }
  });
  return {panel,form,field,title,status,submit,open,calls,answers};
}

test('wiki reading and opening editor do not write; save binds observed version',async()=>{
  const ui=page(); assert.equal(ui.calls.length,0);
  ui.open.events.click(); assert.equal(ui.form.hidden,false); assert.equal(ui.calls.length,0);
  ui.field.value='Corrected knowledge'; await ui.form.events.submit({preventDefault(){}});
  const request=JSON.parse(ui.calls[0].args.body);
  assert.equal(request.operation,'content.post_edit'); assert.equal(request.revision,'observed');
  assert.equal(request.generation,7); assert.equal(request.body,'Corrected knowledge');
  assert.equal(ui.panel.dataset.revision,'new'); assert.equal(ui.panel.dataset.generation,8);
});

test('conflicts preserve the draft and reuse request id when retrying unchanged input',async()=>{
  const ui=page(); ui.field.value='My draft';
  ui.answers.push({status:'error',error:{code:'generation_conflict'}},{status:'error',error:{code:'generation_conflict'}});
  await ui.form.events.submit({preventDefault(){}});
  assert.equal(ui.field.value,'My draft'); assert.ok(ui.status.textContent.includes('Someone edited'));
  assert.equal(ui.panel.dataset.revision,'observed'); assert.equal(ui.submit.disabled,false);
  await ui.form.events.submit({preventDefault(){}});
  assert.equal(JSON.parse(ui.calls[0].args.body).request_id,JSON.parse(ui.calls[1].args.body).request_id);
});

test('new wiki articles use explicit create and preserve title',async()=>{
  const ui=page('topic'); ui.title.value='A topic'; ui.field.value='Knowledge';
  await ui.form.events.submit({preventDefault(){}});
  const request=JSON.parse(ui.calls[0].args.body);
  assert.equal(request.operation,'content.post_create'); assert.equal(request.name,'A topic');
  assert.equal(request.id,'article'); assert.equal(request.revision,undefined);
});
