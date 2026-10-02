"""Execute the live page script against deterministic request and visibility transitions."""

import json
import shutil
import subprocess
from importlib.resources import files

import pytest

NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='live page script checks require Node.js')

HARNESS = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.attributes={};this.dataset={};this.listeners={};this.clientWidth=800;this.textContent='';this.hidden=false;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;}
 setAttribute(key,value){this.attributes[key]=String(value);}
 toggleAttribute(key,force){if(force)this.attributes[key]='';else delete this.attributes[key];}
 addEventListener(event,listener){this.listeners[event]=listener;}
}
const elements=new Map(),documentEvents={},windowEvents={},requests=[],timers=new Map();let timerId=0;
globalThis.document={hidden:false,getElementById:id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);},createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),addEventListener:(name,fn)=>documentEvents[name]=fn};
globalThis.window={addEventListener:(name,fn)=>windowEvents[name]=fn};
globalThis.setTimeout=(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id;};
globalThis.clearTimeout=id=>timers.delete(id);
globalThis.fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
const node=(id,state='unknown',active=null)=>({id,name:id,path:'/@'+id,presence:{state},last_public_activity_at:active});
const snapshot=(nodes=[],edges=[])=>({nodes,edges,events:[],generated_at:'2026-10-02T03:00:00Z',bounded:false});
const succeed=(index,data)=>requests[index].resolve({ok:true,json:async()=>data});
const clickPause=()=>document.getElementById('pause').listeners.click();
const textTree=e=>[e.textContent,...e.children.map(textTree)].join(' ');
"""


def execute(probe):
    page = files('msg.data').joinpath('live-space.html').read_text()
    script = page.split('<script>', 1)[1].split('</script>', 1)[0]
    source = (
        HARNESS
        + '\n'
        + script
        + '\n(async()=>{'
        + probe
        + '})().catch(e=>{console.error(e);process.exitCode=1;});'
    )
    result = subprocess.run([NODE, '-e', source], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_resume_rejects_old_inflight_result_and_starts_immediately():
    execute(r"""
assert.equal(requests.length,1);
clickPause();assert.equal(requests[0].options.signal.aborted,true);
clickPause();assert.equal(requests.length,2);
succeed(0,snapshot([node('old')]));await flush();
assert.equal(document.getElementById('live').textContent,'SYNC');
assert.equal(textTree(document.getElementById('network')).includes('old'),false);
succeed(1,snapshot([node('new')]));await flush();
assert.equal(document.getElementById('live').textContent,'LIVE');
assert.ok(textTree(document.getElementById('network')).includes('new'));
assert.equal([...timers.values()].filter(t=>t.delay===5000).length,1);
assert.equal(requests[1].options.credentials,'omit');
assert.equal(requests[1].options.cache,'no-store');
windowEvents.pagehide();windowEvents.pageshow({persisted:true});assert.equal(requests.length,3);
succeed(2,snapshot([node('returned')]));await flush();
assert.ok(textTree(document.getElementById('network')).includes('returned'));
""")


def test_hidden_request_cannot_overwrite_fresh_data_or_stale_status():
    execute(r"""
succeed(0,snapshot([node('first')]));await flush();
refresh();assert.equal(requests.length,2);
document.hidden=true;documentEvents.visibilitychange();
assert.equal(requests[1].options.signal.aborted,true);
document.hidden=false;documentEvents.visibilitychange();assert.equal(requests.length,3);
requests[1].reject(new Error('old request failed'));await flush();
assert.equal(document.getElementById('live').textContent,'SYNC');
succeed(2,snapshot([node('second')]));await flush();
refresh();requests[3].reject(new Error('offline'));await flush();
assert.equal(document.getElementById('live').textContent,'STALE');
assert.ok(textTree(document.getElementById('network')).includes('second'));
windowEvents.resize();const resize=[...timers.values()].find(t=>t.delay===150);resize.fn();
assert.equal(document.getElementById('live').textContent,'STALE');
assert.ok(document.getElementById('status').textContent.includes('最后更新'));
""")


def test_no_signal_claim_before_first_success_and_presence_is_not_inferred():
    execute(r"""
requests[0].reject(new Error('offline'));await flush();
assert.equal(document.getElementById('live').textContent,'STALE');
assert.ok(document.getElementById('empty-copy').textContent.includes('无法读取'));
refresh();succeed(1,snapshot([node('online','available'),node('busy','busy'),node('poster','unknown','2026-10-02T03:00:00Z'),node('referenced')]));await flush();
const text=textTree(document.getElementById('network'));
assert.ok(text.includes('+ 在线'));assert.ok(text.includes('* 忙碌'));
assert.ok(text.includes('. 公开活动'));assert.ok(text.includes('- 被引用'));
refresh();succeed(2,snapshot());await flush();
assert.equal(document.getElementById('empty').hidden,false);
assert.ok(document.getElementById('empty-copy').textContent.includes('没有可见活动'));
""")


def test_dense_nodes_are_spaced_and_directed_edges_are_coalesced():
    nodes = [f'agent-{index:03}' for index in range(200)]
    execute(
        r"""
const ids="""
        + json.dumps(nodes)
        + r""";
const data=snapshot(ids.map(id=>node(id)),[
{source:ids[0],target:ids[1],kind:'reply_to'},
{source:ids[0],target:ids[1],kind:'quote'},
{source:ids[2],target:ids[2],kind:'repost'}]);
succeed(0,data);await flush();
const scene=document.getElementById('network'),links=scene.children.filter(e=>e.tag==='a');
assert.equal(links.length,200);
const boxes=links.map(e=>e.children.find(c=>c.tag==='rect').attributes);
for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++)assert.ok(Math.abs(Number(boxes[i].x)-Number(boxes[j].x))>=172||Math.abs(Number(boxes[i].y)-Number(boxes[j].y))>=44);
const edges=scene.children.filter(e=>e.tag==='path');assert.equal(edges.length,2);
assert.equal(edges[0].attributes['marker-end'],'url(#arrow)');
assert.ok(textTree(edges[0]).includes('回复 / 引用 · 2 次'));
assert.ok(edges[1].attributes.d.includes(' C '));
assert.equal(links[0].attributes.href,'/@'+ids[0]);
assert.ok(compactName('二十个中文字符在密集图中也应该完整限宽').length<12);
document.getElementById('network-frame').clientWidth=320;
windowEvents.resize();[...timers.values()].find(t=>t.delay===150).fn();
assert.ok(document.getElementById('network').attributes.viewBox.startsWith('0 0 320 '));
"""
    )


def test_small_network_keeps_labels_readable_at_mobile_widths():
    execute(r"""
let requestIndex=0;
for(const width of [280,320,390,600])for(const count of [1,2,8]){
 document.getElementById('network-frame').clientWidth=width;
 if(requestIndex>0)refresh();
 succeed(requestIndex++,snapshot(Array.from({length:count},(_,i)=>node('agent-'+i))));await flush();
 const scene=document.getElementById('network'),sceneWidth=Number(scene.attributes.viewBox.split(' ')[2]);
 assert.ok(14*width/sceneWidth>=14,'mobile labels must not shrink below their authored size');
 const boxes=scene.children.filter(e=>e.tag==='a').map(e=>e.children.find(c=>c.tag==='rect').attributes);
 for(const box of boxes)assert.ok(Number(box.x)>=0&&Number(box.x)+172<=sceneWidth);
 for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++)assert.ok(Math.abs(Number(boxes[i].x)-Number(boxes[j].x))>=172||Math.abs(Number(boxes[i].y)-Number(boxes[j].y))>=44);
}
""")
