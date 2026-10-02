"""Execute the shipped editor script to check draft and motion interactions."""

import json
import shutil
import subprocess

import pytest

from msg.transports.public_board import SCRIPT, html

HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(value=''){this.value=value;this.textContent='';this.disabled=false;this.hidden=false;this.attrs={};this.events={};}
 setAttribute(name,value){this.attrs[name]=value;}
 addEventListener(name,listener){(this.events[name]??=[]).push(listener);}
 focus(){this.focused=true;}
 async emit(name,event={}){for(const listener of this.events[name]??[])await listener(event);}
}
const fields={svg:new Element('old-svg'),text:new Element('old-text')};
const edit=new Element(),refresh=new Element(),save=new Element(),pause=new Element(),status=new Element(),image=new Element();
const version=new Element(),content=new Element(),quota=new Element(),size=new Element(),form=new Element();form.hidden=true;
const selectors={'form':form,'[role=status]':status,'img':image,'[data-edit]':edit,'[data-refresh]':refresh,'[type=submit]':save,
 '[data-pause]':pause,'[data-version]':version,'[data-content]':content,'[data-quota]':quota,'[data-size]':size,
 '[name=svg]':fields.svg,'[name=text]':fields.text};
form.querySelector=selector=>selectors[selector];
const panel={dataset:{generation:'0',signedIn:'true',csrf:'csrf'},querySelector:selector=>selectors[selector]};
const motion=new Element();motion.matches=!!input.reduced;
const windowEvents=new Element(),requests=[],snapshots=[];
let nextResponse,resolveFetch,running,id=0;
const context={document:{getElementById:()=>panel},matchMedia:()=>motion,TextEncoder,
 crypto:{randomUUID:()=>`request-${++id}`},AbortSignal:{timeout:()=>undefined},
 addEventListener:(...args)=>windowEvents.addEventListener(...args),location:{assign:()=>{}},
 fetch:async(url,options={})=>{
   requests.push({url,body:options.body?JSON.parse(options.body):null});
   const response=nextResponse;nextResponse=null;
   if(response?.defer)return await new Promise(resolve=>{resolveFetch=resolve;});
   if(response?.throw)throw new Error('network_failed');
   if(!response)throw new Error('unexpected_fetch');
   return {ok:response.ok??true,json:async()=>response.body};
 }};
vm.runInNewContext(input.script,context);
const snapshot=()=>({svg:fields.svg.value,text:fields.text.value,generation:panel.dataset.generation,hidden:form.hidden,
 expanded:edit.attrs['aria-expanded'],pressed:pause.attrs['aria-pressed'],image:image.src,status:status.textContent,
 saveDisabled:save.disabled,refreshDisabled:refresh.disabled,svgDisabled:fields.svg.disabled,textDisabled:fields.text.disabled,
 size:size.textContent,requests:structuredClone(requests)});
(async()=>{
 for(const action of input.actions){
   if(action.kind==='input'){fields[action.name].value=action.value;await form.emit('input');}
   if(action.kind==='edit')await edit.emit('click');
   if(action.kind==='pause')await pause.emit('click');
   if(action.kind==='motion'){motion.matches=action.matches;await motion.emit('change',{matches:action.matches});}
   if(action.kind==='refresh'){nextResponse=action.response;await refresh.emit('click');}
   if(action.kind==='submit'){nextResponse=action.response;await form.emit('submit',{preventDefault(){}});}
   if(action.kind==='start-submit'){nextResponse={defer:true};running=form.emit('submit',{preventDefault(){}});}
   if(action.kind==='finish-submit'){resolveFetch({ok:true,json:async()=>action.body});await running;}
   if(action.kind==='leave'){let prevented=false;await windowEvents.emit('beforeunload',{preventDefault(){prevented=true;}});snapshots.push({prevented});}
   if(action.kind==='snapshot')snapshots.push(snapshot());
 }
 process.stdout.write(JSON.stringify({snapshots,final:snapshot()}));
})().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
"""


def run_editor(actions, *, reduced=False):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the shipped browser script')
    result = subprocess.run(
        [node, '-e', HARNESS],
        input=json.dumps({'script': SCRIPT, 'actions': actions, 'reduced': reduced}),
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def value(generation, *, svg='new-svg', text='new-text'):
    return {
        'generation': generation,
        'svg': svg,
        'text': text,
        'quota': {'hour_count': 1, 'day_count': 1},
    }


def test_conflict_refresh_keeps_draft_and_only_submits_changed_field():
    saved = value(2, text='my-draft')
    result = run_editor([
        {'kind': 'edit'},
        {'kind': 'input', 'name': 'text', 'value': 'my-draft'},
        {'kind': 'leave'},
        {'kind': 'refresh', 'response': {'body': value(1)}},
        {'kind': 'snapshot'},
        {'kind': 'submit', 'response': {'body': {'status': 'ok', 'data': saved}}},
        {'kind': 'leave'},
    ])
    protected, refreshed, clean = result['snapshots']
    assert protected['prevented'] and not clean['prevented']
    assert refreshed['text'] == 'my-draft'
    assert refreshed['svg'] == 'new-svg' and refreshed['generation'] == '1'
    payload = result['final']['requests'][1]['body']
    assert payload['generation'] == 1 and payload['text'] == 'my-draft'
    assert 'svg' not in payload
    assert result['final']['hidden'] and result['final']['expanded'] == 'false'


def test_save_serializes_refresh_and_reuses_request_id_after_failure():
    result = run_editor([
        {'kind': 'edit'},
        {'kind': 'input', 'name': 'text', 'value': 'my-draft'},
        {'kind': 'start-submit'},
        {'kind': 'snapshot'},
        {'kind': 'refresh'},
        {'kind': 'submit'},
        {
            'kind': 'finish-submit',
            'body': {'status': 'ok', 'data': value(1, text='my-draft', svg='old-svg')},
        },
    ])
    saving = result['snapshots'][0]
    assert all(
        saving[name] for name in ('saveDisabled', 'refreshDisabled', 'svgDisabled', 'textDisabled')
    )
    assert len(result['final']['requests']) == 1
    assert not result['final']['saveDisabled'] and not result['final']['textDisabled']
    failed = run_editor([
        {'kind': 'edit'},
        {'kind': 'input', 'name': 'text', 'value': 'my-draft'},
        {'kind': 'submit', 'response': {'throw': True}},
        {
            'kind': 'submit',
            'response': {'ok': False, 'body': {'error': {'code': 'credential_ceiling'}}},
        },
    ])['final']
    assert (
        failed['requests'][0]['body']['request_id'] == failed['requests'][1]['body']['request_id']
    )
    assert failed['text'] == 'my-draft' and not failed['hidden']
    assert '站点授权' in failed['status'] and '仅重新登录可能无效' in failed['status']


def test_motion_preference_and_editor_state_are_accessible():
    result = run_editor(
        [
            {'kind': 'snapshot'},
            {'kind': 'edit'},
            {'kind': 'pause'},
            {'kind': 'snapshot'},
            {'kind': 'motion', 'matches': True},
        ],
        reduced=True,
    )
    initial, playing = result['snapshots']
    assert initial['pressed'] == 'true' and 'motion=still' in initial['image']
    assert playing['expanded'] == 'true' and playing['pressed'] == 'false'
    assert 'motion=still' not in playing['image']
    assert result['final']['pressed'] == 'true' and 'motion=still' in result['final']['image']
    rendered = html()
    assert 'aria-controls="public-board-editor"' in rendered
    assert '&amp;motion=still' in rendered and 'aria-describedby="public-board-size"' in rendered


def test_unchanged_form_never_sends_an_empty_patch():
    result = run_editor([{'kind': 'edit'}, {'kind': 'submit'}])['final']
    assert result['requests'] == []
    assert '内容没有变化' in result['status']
