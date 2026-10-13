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
 getAttribute(name){return name==='src'?this.src:this.attrs[name];}
 addEventListener(name,listener){(this.events[name]??=[]).push(listener);}
 dispatchEvent(event){this.emit(event.type,event);return true;}
 focus(){this.focused=true;}
 async emit(name,event={}){for(const listener of this.events[name]??[])await listener(event);}
}
const fields={svg:new Element('old-svg'),text:new Element('old-text')};
const edit=new Element(),refresh=new Element(),save=new Element(),pause=new Element(),status=new Element(),image=new Element();
const imageLoads=[];image.attrs.src='/_public-board/art.svg?v=0&motion=still';
Object.defineProperty(image,'src',{get(){return this.attrs.src;},set(value){this.attrs.src=value;imageLoads.push(value);}});
const success=new Element();success.hidden=true;
const menu=new Element(),menuToggle=new Element(),close=new Element();menu.open=false;
menu.querySelector=()=>menuToggle;menu.contains=target=>[menu,menuToggle,edit,pause].includes(target);
const version=new Element(),content=new Element(),quota=new Element(),size=new Element(),form=new Element();form.hidden=true;
const selectors={'form':form,'[role=status]':status,'img':image,'[data-edit]':edit,'[data-refresh]':refresh,'[type=submit]':save,
 '[data-pause]':pause,'[data-version]':version,'[data-content]':content,'[data-quota]':quota,'[data-size]':size,
 '[data-menu]':menu,'[data-close]':close,
 '[data-success]':success,
 '[name=svg]':fields.svg,'[name=text]':fields.text};
form.querySelector=selector=>selectors[selector];
const panel=new Element();panel.dataset={generation:'0',signedIn:'true',csrf:'csrf'};panel.querySelector=selector=>selectors[selector];
const motion=new Element();motion.matches=!!input.reduced;
const windowEvents=new Element(),requests=[],snapshots=[];
let nextResponse,resolveFetch,running,id=0;
const timers=new Map();let timerId=0;
const document=new Element();document.hidden=false;document.getElementById=()=>panel;let intersect;
const context={document,matchMedia:()=>motion,TextEncoder,CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
 setTimeout:callback=>{timers.set(++timerId,callback);return timerId;},clearTimeout:id=>timers.delete(id),
 crypto:{randomUUID:()=>`request-${++id}`},AbortSignal:{timeout:()=>undefined},
 addEventListener:(...args)=>windowEvents.addEventListener(...args),location:{assign:()=>{}},
 fetch:async(url,options={})=>{
   requests.push({url,body:options.body?JSON.parse(options.body):null});
   const response=nextResponse;nextResponse=null;
   if(response?.defer)return await new Promise(resolve=>{resolveFetch=resolve;});
   if(response?.throw)throw new Error('network_failed');
   if(!response)throw new Error('unexpected_fetch');
   return {ok:response.ok??true,json:async()=>response.body,
     headers:{get:name=>response.headers?.[name]??null}};
 }};
if(input.observe)context.IntersectionObserver=class {
 constructor(callback){intersect=callback;}
 observe(target){if(target!==image)throw new Error('observer_target');}
};
vm.runInNewContext(input.script,context);
const snapshot=()=>({svg:fields.svg.value,text:fields.text.value,generation:panel.dataset.generation,hidden:form.hidden,
 menuOpen:menu.open,menuFocused:!!menuToggle.focused,textFocused:!!fields.text.focused,
 successHidden:success.hidden,successText:success.textContent,
 expanded:edit.attrs['aria-expanded'],pressed:pause.attrs['aria-pressed'],image:image.src,imageLoads:[...imageLoads],status:status.textContent,
 saveDisabled:save.disabled,refreshDisabled:refresh.disabled,svgDisabled:fields.svg.disabled,textDisabled:fields.text.disabled,
 size:size.textContent,quota:quota.textContent,requests:structuredClone(requests)});
(async()=>{
 for(const action of input.actions){
   if(action.kind==='input'){fields[action.name].value=action.value;await form.emit('input');}
   if(action.kind==='edit')await edit.emit('click');
   if(action.kind==='open-menu')menu.open=true;
   if(action.kind==='close-editor')await close.emit('click');
   if(action.kind==='escape')await panel.emit('keydown',{key:'Escape',preventDefault(){}});
   if(action.kind==='outside-click')await windowEvents.emit('click',{target:new Element()});
   if(action.kind==='expire-success'){for(const callback of [...timers.values()])callback();}
   if(action.kind==='pause')await pause.emit('click');
   if(action.kind==='motion'){motion.matches=action.matches;await motion.emit('change',{matches:action.matches});}
   if(action.kind==='visibility'){document.hidden=action.hidden;await document.emit('visibilitychange');}
   if(action.kind==='intersection')intersect([{isIntersecting:action.visible}]);
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


def run_editor(actions, *, reduced=False, observe=False):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the shipped browser script')
    result = subprocess.run(
        [node, '-e', HARNESS],
        input=json.dumps({
            'script': SCRIPT,
            'actions': actions,
            'reduced': reduced,
            'observe': observe,
        }),
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


def test_public_only_refresh_preserves_user_draft_without_inventing_zero_quota():
    public = {**value(3), 'quota': None}
    result = run_editor([
        {'kind': 'edit'},
        {'kind': 'input', 'name': 'text', 'value': 'my-draft'},
        {
            'kind': 'refresh',
            'response': {
                'body': public,
                'headers': {'X-Msg-Public-Fallback': 'credential-ceiling'},
            },
        },
    ])['final']
    assert result['text'] == 'my-draft' and result['svg'] == 'new-svg'
    assert result['generation'] == '3' and '已读取公共内容' in result['status']
    assert '不代表额度已用完' in result['quota'] and '/5' not in result['quota']
    assert len(result['requests']) == 1 and result['requests'][0]['body'] is None


def test_malformed_refresh_leaves_all_draft_fields_and_generation_untouched():
    result = run_editor([
        {'kind': 'edit'},
        {'kind': 'input', 'name': 'text', 'value': 'my-draft'},
        {'kind': 'refresh', 'response': {'body': {'svg': 'new-svg', 'generation': 3}}},
    ])['final']
    assert result['text'] == 'my-draft' and result['svg'] == 'old-svg'
    assert result['generation'] == '0' and '草稿未变' in result['status']


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
    assert not result['final']['successHidden'] and '已保存' in result['final']['successText']


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


def test_options_are_discreet_and_editor_close_preserves_draft():
    result = run_editor([
        {'kind': 'snapshot'},
        {'kind': 'open-menu'},
        {'kind': 'edit'},
        {'kind': 'input', 'name': 'text', 'value': 'unfinished draft'},
        {'kind': 'snapshot'},
        {'kind': 'close-editor'},
        {'kind': 'snapshot'},
        {'kind': 'open-menu'},
        {'kind': 'escape'},
        {'kind': 'snapshot'},
        {'kind': 'open-menu'},
        {'kind': 'outside-click'},
    ])
    initial, editing, closed, escaped = result['snapshots']
    assert initial['hidden'] and not initial['menuOpen']
    assert not editing['hidden'] and not editing['menuOpen'] and editing['textFocused']
    assert closed['hidden'] and closed['text'] == 'unfinished draft' and closed['menuFocused']
    assert not escaped['menuOpen'] and escaped['menuFocused']
    assert not result['final']['menuOpen'] and result['final']['text'] == 'unfinished draft'
    rendered = html()
    menu = rendered.split('<details class="public-board-menu"', 1)[1].split('</details>', 1)[0]
    assert ' open' not in menu and 'data-edit' in menu and 'data-pause' in menu
    assert '>⋯</summary>' in menu
    editor = rendered.split('<form id="public-board-editor" hidden>', 1)[1]
    assert 'data-version' in editor and 'data-quota' in editor and '每账号每小时 5 次' in editor
    assert '禁止批量' in editor and 'SVG ≤ 16 KiB' in editor
    assert (
        '点击「编辑公共栏」' not in rendered and '所有已登录用户和 Agent 都可修改' not in rendered
    )
    assert '<h1>' not in rendered


def test_success_is_visible_outside_closed_editor_then_returns_to_quiet_default():
    result = run_editor([
        {'kind': 'snapshot'},
        {'kind': 'edit'},
        {'kind': 'input', 'name': 'text', 'value': 'published text'},
        {
            'kind': 'submit',
            'response': {
                'body': {'status': 'ok', 'data': value(1, text='published text', svg='old-svg')}
            },
        },
        {'kind': 'snapshot'},
        {'kind': 'expire-success'},
    ])
    initial, saved = result['snapshots']
    assert initial['successHidden'] and not initial['successText']
    assert saved['hidden'] and not saved['successHidden'] and '已保存' in saved['successText']
    assert result['final']['successHidden'] and not result['final']['successText']
    rendered = html()
    assert rendered.index('data-success hidden') < rendered.index(
        '<form id="public-board-editor" hidden>'
    )
    assert 'aria-atomic="true" data-success' in rendered


def test_art_visibility_resumes_only_after_page_and_image_are_visible():
    result = run_editor(
        [
            {'kind': 'snapshot'},
            {'kind': 'intersection', 'visible': True},
            {'kind': 'snapshot'},
            {'kind': 'intersection', 'visible': True},
            {'kind': 'visibility', 'hidden': True},
            {'kind': 'visibility', 'hidden': True},
            {'kind': 'snapshot'},
            {'kind': 'intersection', 'visible': False},
            {'kind': 'visibility', 'hidden': False},
            {'kind': 'snapshot'},
            {'kind': 'intersection', 'visible': True},
        ],
        observe=True,
    )
    initial, playing, hidden, offscreen = result['snapshots']
    assert 'motion=still' in initial['image'] and initial['imageLoads'] == []
    assert 'motion=still' not in playing['image']
    assert 'motion=still' in hidden['image'] and hidden['pressed'] == 'false'
    assert offscreen['imageLoads'] == hidden['imageLoads']
    assert 'motion=still' not in result['final']['image']
    assert len(result['final']['imageLoads']) == 3


def test_art_user_pause_and_reduced_intent_survive_automatic_resume():
    paused = run_editor(
        [
            {'kind': 'intersection', 'visible': True},
            {'kind': 'pause'},
            {'kind': 'intersection', 'visible': False},
            {'kind': 'visibility', 'hidden': True},
            {'kind': 'motion', 'matches': True},
            {'kind': 'motion', 'matches': False},
            {'kind': 'visibility', 'hidden': False},
            {'kind': 'intersection', 'visible': True},
        ],
        observe=True,
    )['final']
    assert paused['pressed'] == 'true' and 'motion=still' in paused['image']
    assert len(paused['imageLoads']) == 2
    reduced = run_editor(
        [
            {'kind': 'intersection', 'visible': True},
            {'kind': 'intersection', 'visible': False},
            {'kind': 'visibility', 'hidden': True},
            {'kind': 'visibility', 'hidden': False},
            {'kind': 'intersection', 'visible': True},
        ],
        reduced=True,
        observe=True,
    )['final']
    assert reduced['pressed'] == 'true' and reduced['imageLoads'] == []


def test_art_same_revision_refresh_does_not_restart_image():
    result = run_editor(
        [
            {'kind': 'intersection', 'visible': True},
            {'kind': 'refresh', 'response': {'body': value(0)}},
            {'kind': 'refresh', 'response': {'body': value(1)}},
            {'kind': 'refresh', 'response': {'body': value(1)}},
        ],
        observe=True,
    )['final']
    assert result['imageLoads'] == [
        '/_public-board/art.svg?v=0',
        '/_public-board/art.svg?v=1',
    ]
