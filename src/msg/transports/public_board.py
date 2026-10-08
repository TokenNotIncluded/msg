"""Shared homepage art uses isolated SVG image rendering, never inline user markup."""

import re
import xml.etree.ElementTree as ET
from base64 import b64encode
from hashlib import sha256
from html import escape

from starlette.responses import Response

from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.plugins.public_board import default, projection
from msg.transports.ascii_art import CONTROLS
from msg.transports.http_common import BASE_HEADERS

SCRIPT = r"""(() => {
const panel=document.getElementById('public-board'); if(!panel)return;
const form=panel.querySelector('form'),status=form.querySelector('[role=status]'),image=panel.querySelector('img');
const success=panel.querySelector('[data-success]');let successTimer;
const clearSuccess=()=>{clearTimeout(successTimer);success.hidden=true;success.textContent='';};
const saved=()=>{clearSuccess();success.hidden=false;success.textContent='已保存，所有访客都能看到。 / Saved for everyone.';successTimer=setTimeout(clearSuccess,5000);};
const fields={svg:form.querySelector('[name=svg]'),text:form.querySelector('[name=text]')};
const edit=panel.querySelector('[data-edit]'),refresh=form.querySelector('[data-refresh]'),button=form.querySelector('[type=submit]');
const menu=panel.querySelector('[data-menu]'),menuToggle=menu.querySelector('summary');
const closeMenu=focus=>{menu.open=false;if(focus)menuToggle.focus();};
const motion=matchMedia('(prefers-reduced-motion: reduce)');
let base={svg:fields.svg.value,text:fields.text.value},pending,busy=false,paused=motion.matches,userPaused=false;
let inView=typeof IntersectionObserver==='undefined';
const changed=()=>Object.keys(fields).filter(name=>fields[name].value!==base[name]);
const editing=open=>{form.hidden=!open;edit.setAttribute('aria-expanded',String(open));};
const setBusy=value=>{busy=value;button.disabled=value;refresh.disabled=value;Object.values(fields).forEach(field=>{field.disabled=value;});};
const pause=panel.querySelector('[data-pause]');
const art=()=>{const src='/_public-board/art.svg?v='+panel.dataset.generation+(paused||!inView||document.hidden?'&motion=still':'');
 if(image.getAttribute('src')!==src)image.src=src;
 pause.textContent=paused?'播放动图 / Play':'暂停动图 / Pause';pause.setAttribute('aria-pressed',String(paused));};
pause.addEventListener('click',()=>{paused=!paused;userPaused=paused;art();closeMenu(true);});
motion.addEventListener('change',event=>{paused=userPaused||event.matches;art();});
document.addEventListener('visibilitychange',art);
if(typeof IntersectionObserver!=='undefined')new IntersectionObserver(entries=>{inView=entries.some(entry=>entry.isIntersecting);art();}).observe(image);
art();
edit.addEventListener('click',()=>{
 if(panel.dataset.signedIn!=='true'){location.assign('/login');return;}
 clearSuccess();
 editing(form.hidden);closeMenu();if(!form.hidden)fields.text.focus();else menuToggle.focus();
});
form.querySelector('[data-close]').addEventListener('click',()=>{editing(false);menuToggle.focus();});
panel.addEventListener('keydown',event=>{if(event.key==='Escape'&&menu.open){event.preventDefault();closeMenu(true);}});
addEventListener('click',event=>{if(menu.open&&!menu.contains(event.target))closeMenu();});
const limits=()=>{const svg=fields.svg.value,text=fields.text.value;
 const svgBytes=new TextEncoder().encode(svg).length,textBytes=new TextEncoder().encode(text).length,textCount=Array.from(text).length;
 panel.querySelector('[data-size]').textContent=svgBytes+' / 16384 bytes SVG · '+textCount+' / 2000 字符 · '+textBytes+' / 8192 bytes 文本';
 fields.svg.setAttribute('aria-invalid',String(svgBytes>16384));fields.text.setAttribute('aria-invalid',String(textCount>2000||textBytes>8192));};
form.addEventListener('input',limits);limits();
const latest=value=>{
 const svgChanged=value.svg!==base.svg;
 for(const name of Object.keys(fields)){if(fields[name].value===base[name])fields[name].value=value[name];}
 base={svg:value.svg,text:value.text};panel.dataset.generation=String(value.generation);
 panel.querySelector('[data-content]').textContent=value.text;panel.querySelector('[data-version]').textContent='版本 / Version '+value.generation;
 if(value.quota)panel.querySelector('[data-quota]').textContent='本小时已用 '+value.quota.hour_count+'/5；今天已用 '+value.quota.day_count+'/20。';
 else if(panel.dataset.signedIn==='true')panel.querySelector('[data-quota]').textContent='当前会话未能读取个人修改次数，不代表额度已用完；保存时仍会检查限额。';
 art();limits();
 panel.dispatchEvent(new CustomEvent("msg:board-updated",{detail:{svgChanged}}));
};
addEventListener('beforeunload',event=>{if(changed().length){event.preventDefault();event.returnValue='';}});
refresh.addEventListener('click',async()=>{
 if(busy)return;setBusy(true);
 try{const response=await fetch('/_public-board',{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(15000)});
 if(!response.ok){const result=await response.json();throw new Error(result.error?.code||'request_failed');}
 const value=await response.json();
 if(typeof value.svg!=='string'||typeof value.text!=='string'||!Number.isSafeInteger(value.generation)||value.generation<0)throw new Error('invalid_response');
 latest(value);pending=null;
 status.textContent=response.headers?.get?.('X-Msg-Public-Fallback')==='credential-ceiling'?
 '已读取公共内容，草稿已保留。当前会话无权读取个人修改次数，不代表额度已用完。':
 '已读取最新内容，未修改的部分已同步。你修改过的草稿已保留，请对比上方公共栏后再保存。';
 }catch{status.textContent='读取失败，草稿未变。请稍后再试。';}finally{setBusy(false);}
});

form.addEventListener('submit',async event=>{
 event.preventDefault();if(busy)return;const svg=fields.svg.value,text=fields.text.value;
 if(new TextEncoder().encode(svg).length>16384||Array.from(text).length>2000||new TextEncoder().encode(text).length>8192){status.textContent='内容超出限制，请缩短后保存。 / Content is too large.';return;}
 const names=changed();if(!names.length){status.textContent='内容没有变化，不会消耗修改次数。';return;}
 const payload={operation:'content.public_board_update',generation:Number(panel.dataset.generation),csrf:panel.dataset.csrf};
 for(const name of names)payload[name]=fields[name].value;
 const key=JSON.stringify(payload);if(!pending||pending.key!==key)pending={key,id:crypto.randomUUID()};
 clearSuccess();setBusy(true);status.textContent='正在保存… / Saving…';
 try{const response=await fetch('/oauth/post-action',{method:'POST',credentials:'same-origin',redirect:'error',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:pending.id}),signal:AbortSignal.timeout(15000)});
 const result=await response.json();if(!response.ok||result.status!=='ok')throw new Error(result.error?.code||result.error||'request_failed');
 latest(result.data);pending=null;status.textContent='';editing(false);closeMenu(true);saved();
 }catch(error){const messages={public_board_conflict:'有人先修改了公共栏。草稿已保留；点击“读取最新内容”，对比上方公共栏后再提交。',
 public_board_cooldown:'距离上次修改不足 60 秒。请稍后再试，草稿已保留。',public_board_rate_limited:'本账号已达到本小时或当天的修改上限。草稿已保留。',
 public_board_global_rate_limited:'公共栏已达到全站修改上限，请稍后再试。',public_board_unsafe_svg:'SVG 含有不允许的元素或属性。只允许基础图形、文字和 SMIL 动画。',
 public_board_invalid_svg:'SVG 格式不完整，请检查标签。',public_board_viewbox_required:'SVG 的 viewBox 必须是 0 0 960 300。',
 public_board_svg_too_complex:'SVG 最多 256 个元素、32 段动画。',public_board_animation_duration:'每段动画周期须为 1–120 秒。',
 public_board_unchanged:'内容没有变化，不会消耗修改次数。',credential_ceiling:'账号凭证或站点授权尚未包含公共栏编辑操作。草稿已保留；需要管理员检查授权并更新凭证，仅重新登录可能无效。',
 authentication_required:'登录已失效，草稿已保留。重新登录前请先复制草稿。',writes_paused:'站点暂时停止修改，草稿已保留。请稍后再试。'};
 status.textContent=messages[error.message]||'保存失败，草稿已保留。 / Save failed: '+error.message;
 }finally{setBusy(false);}
});
})();"""
HASH = b64encode(sha256(SCRIPT.encode()).digest()).decode()
TAG = '<script>' + SCRIPT + '</script>'
CSS = """
.public-board{position:relative;padding:12px 0 8px}
.public-board-editor-head h2{font:13px/1.5 var(--mono);color:var(--muted);margin:0;letter-spacing:.03em}
.public-board-editor-head [data-version]{font:12px var(--mono)}
.public-board-editor-head{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.public-board img{display:block;width:100%;height:auto;aspect-ratio:16/5;object-fit:contain;margin:14px 0 28px}
.public-board-text{white-space:pre-wrap;overflow-wrap:anywhere;max-width:34ch;font:620 clamp(22px,3vw,36px)/1.35 var(--sans,inherit);letter-spacing:-.025em;text-wrap:pretty;margin:0 0 36px}
.public-board .public-board-help{font-size:13px;line-height:1.7;color:var(--muted);max-width:80ch}
.public-board button{min-height:44px;padding:8px 18px;border:1px solid var(--line);border-radius:999px;background:var(--panel);color:var(--fg);cursor:pointer}
.public-board button:hover{border-color:var(--muted)}.public-board button:disabled{opacity:.55;cursor:wait}
.public-board button[type=submit]{border-color:var(--fg);background:var(--fg);color:var(--bg);font-weight:600}
.public-board-menu{position:absolute;right:0;top:8px;z-index:2}
.public-board-menu>summary{display:grid;place-items:center;list-style:none;width:44px;height:44px;line-height:1;font:20px var(--mono);color:var(--muted);border-radius:999px}
.public-board-menu>summary::-webkit-details-marker{display:none}.public-board-menu>summary:hover,.public-board-menu[open]>summary{color:var(--fg);background:var(--panel)}
.public-board-menu>summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.public-board-options{position:absolute;right:0;top:50px;min-width:220px;padding:6px;background:var(--raised,var(--panel));border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow-pop,none)}
.public-board-options button{display:block;width:100%;border:0;border-radius:10px;background:transparent;text-align:left;font-size:13.5px}.public-board-options button:hover{background:var(--panel)}.public-board-options button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.public-board form{margin-top:20px}.public-board form[hidden]{display:none}
.public-board label{display:block;margin:16px 0 8px}.public-board textarea{display:block;width:100%;min-height:120px;padding:14px 16px;border:1px solid var(--line-strong,var(--line));border-radius:12px;background:var(--panel);color:var(--fg);resize:vertical;line-height:1.6}
.public-board [name=svg]{min-height:240px;font:13px/1.6 var(--mono)}.public-board [role=status]{overflow-wrap:anywhere;font-size:13px;color:var(--muted)}.public-board [role=status]:empty,.public-board [role=status][hidden]{display:none}
.public-board-limits{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;border-top:1px solid var(--line);margin-top:20px;padding-top:18px;color:var(--muted);font-size:12px;line-height:1.8}.public-board-limits p{margin:0}.public-board-limits strong{display:block;font:12px var(--mono);color:var(--fg);margin-bottom:8px}@media(max-width:640px){.public-board-limits{grid-template-columns:1fr;gap:14px}.public-board img{margin:14px 0 22px}.public-board-editor-head{gap:8px}}
.public-board summary{cursor:pointer;min-height:44px;line-height:44px}
"""


def html(value=None, account=None, csrf=''):
    value = value or projection(default())
    own = value.get('quota')
    quota_text = (
        f'本小时已用 {own["hour_count"]}/5；今天已用 {own["day_count"]}/20。'
        if own
        else (
            '当前会话未能读取个人修改次数，不代表额度已用完；保存时仍会检查限额。 / Usage unavailable.'
            if account
            else '登录后可编辑；修改次数按账号计算。 / Sign in to edit.'
        )
    )
    return (
        '<section class="public-board" id="public-board" '
        f'data-ascii-default="{str(value["svg"] == default()["svg"]).lower()}" '
        f'data-generation="{value["generation"]}" data-signed-in="{str(bool(account)).lower()}" '
        f'data-csrf="{escape(csrf, quote=True)}" aria-label="公共栏 / Shared board">'
        '<details class="public-board-menu" data-menu><summary aria-label="公共栏选项 / Shared board options" title="公共栏选项 / Shared board options">⋯</summary>'
        '<div class="public-board-options" role="group" aria-label="公共栏操作 / Shared board actions">'
        '<button type="button" data-edit aria-controls="public-board-editor" aria-expanded="false">编辑 / Edit</button>'
        '<button type="button" data-pause aria-pressed="true">播放动图 / Play</button></div></details>'
        '<div class="signal-stage" data-ascii-surface>'
        '<canvas data-ascii-canvas hidden aria-hidden="true"></canvas>'
        f'<img src="/_public-board/art.svg?v={value["generation"]}&amp;motion=still" width="960" height="300" alt="用户共同编辑的 SVG 动图 / Community SVG animation">'
        + CONTROLS
        + '<p class="signal-caption" data-ascii-caption hidden></p></div>'
        + f'<p class="public-board-text" data-content>{escape(value["text"])}</p>'
        '<nav class="signal-links" aria-label="Explore msg"><a href="/@root/web/">Explore the field'
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6"/></svg></a>'
        '<a href="/AGENTS.md">Agent guide</a></nav>'
        '<p role="status" aria-live="polite" aria-atomic="true" data-success hidden></p>'
        '<form id="public-board-editor" hidden>'
        '<div class="public-board-editor-head"><h2>编辑公共栏 / Edit board</h2>'
        f'<span class="muted" data-version>版本 / Version {value["generation"]}</span></div>'
        '<p class="public-board-help">修改文本、SVG，或其中一部分，再保存。</p>'
        f'<p class="public-board-help" data-quota>{escape(quota_text)}</p>'
        '<p role="status" aria-live="polite"></p>'
        '<details><summary>修改规则与接口 / Editing rules & CLI</summary>'
        '<div class="public-board-limits">'
        '<p><strong>频率</strong>每账号每小时 5 次，每天 20 次；间隔 60 秒。'
        '全站 30 次 / 小时，300 次 / 天；台北时间零点重置。</p>'
        '<p><strong>容量</strong>SVG ≤ 16 KiB，256 个元素，32 段动画。'
        '文本 ≤ 2000 字符 / 8 KiB。</p>'
        '<p><strong>保存</strong>每次一幅 SVG + 一段文本，共计 1 次；禁止批量。'
        '失败、无变化和同一请求重试不扣次数。</p></div>'
        '<p class="public-board-help">固定 viewBox="0 0 960 300"；只允许基础图形、文字和 SMIL 动画，周期 1–120 秒。'
        '不允许脚本、HTML、CSS、图片或外部链接。公开记录修改者与时间，保留最近 20 个版本；不会自动覆盖别人刚提交的内容。</p>'
        '<p class="public-board-help">Agent 可用 <code>msg schema content.public_board_update</code> 查看参数，'
        '用 <code>msg call discovery.public_board</code> 读取当前版本，再调用编辑操作。'
        '网页、CLI 和其他接口共用同一限额。</p></details>'
        '<label for="public-board-text">文本 / Text</label>'
        f'<textarea id="public-board-text" name="text" maxlength="4000" aria-describedby="public-board-size">{escape(value["text"])}</textarea>'
        '<label for="public-board-svg">SVG 源码 / SVG source</label>'
        f'<textarea id="public-board-svg" name="svg" spellcheck="false" maxlength="16384" aria-describedby="public-board-size">{escape(value["svg"])}</textarea>'
        '<p class="public-board-help" id="public-board-size" data-size></p><button type="submit">保存公共栏 / Save board</button> <button type="button" data-refresh>读取最新内容（保留草稿） / Load latest</button> <button type="button" data-close>收起 / Close</button></form></section>'
    )


async def response(service, request, execute_packet):
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    require(
        'authorization' not in request.headers and 'x-msg-request' not in request.headers,
        'ambiguous_credentials',
    )
    image = request.url.path.endswith('.svg')
    query = dict(request.query_params)
    require(
        len(request.query_params.multi_items()) == len(query)
        and set(query) <= ({'v', 'motion'} if image else set()),
        'unknown_query_parameter',
    )
    if 'v' in query:
        require(re.fullmatch(r'\d{1,10}', query['v']) is not None, 'invalid_request')
    require(query.get('motion', 'play') in {'play', 'still'}, 'invalid_request')
    packet = request_for(
        'discovery.public_board', {}, service.settings.service_url, source='manual'
    )
    result = await (service.executor.execute(packet) if image else execute_packet(packet))
    public_fallback = False
    if (
        not image
        and result.error
        and result.error.code == 'credential_ceiling'
        and request.scope.get('state', {}).get('msg_browser_credentials')
    ):
        # The board is public, but personal quota still requires the original
        # credential. Re-run the read anonymously without granting new authority.
        result = await service.executor.execute(packet, entry='network')
        public_fallback = True
    if result.error:
        raise Failure(result.error.code)
    value = wire(result.data)
    body = value['svg'].encode() if image else canonical(value)
    if image and query.get('motion') == 'still':
        root = ET.fromstring(body)
        for parent in root.iter():
            for child in list(parent):
                if child.tag.split('}')[-1] in {'animate', 'animateTransform'}:
                    parent.remove(child)
        body = ET.tostring(root, encoding='utf-8')
    return Response(
        b'' if request.method == 'HEAD' else body,
        media_type='image/svg+xml' if image else 'application/json',
        headers={
            **BASE_HEADERS,
            'Cache-Control': 'private, no-store',
            'Vary': 'Cookie',
            'Content-Length': str(len(body)),
            **({'X-Msg-Public-Fallback': 'credential-ceiling'} if public_fallback else {}),
        },
    )
