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
from msg.transports.http_common import BASE_HEADERS

SCRIPT = r"""(() => {
const panel=document.getElementById('public-board'); if(!panel)return;
const form=panel.querySelector('form'),status=panel.querySelector('[role=status]'),image=panel.querySelector('img');
let pending,paused=matchMedia('(prefers-reduced-motion: reduce)').matches;
const pause=panel.querySelector('[data-pause]');
const art=()=>{image.src='/_public-board/art.svg?v='+panel.dataset.generation+(paused?'&motion=still':'');pause.textContent=paused?'播放动图 / Play':'暂停动图 / Pause';};
pause.addEventListener('click',()=>{paused=!paused;art();});art();
panel.querySelector('[data-edit]').addEventListener('click',()=>{
 if(panel.dataset.signedIn!=='true'){location.assign('/login');return;}
 form.hidden=!form.hidden;if(!form.hidden)form.querySelector('[name=text]').focus();
});
const limits=()=>{const svg=form.querySelector('[name=svg]').value,text=form.querySelector('[name=text]').value;
 panel.querySelector('[data-size]').textContent=new TextEncoder().encode(svg).length+' / 16384 bytes SVG · '+Array.from(text).length+' / 2000 字符';};
form.addEventListener('input',limits);limits();
form.querySelector('[data-refresh]').addEventListener('click',async()=>{
 try{const response=await fetch('/_public-board',{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(15000)});
 if(!response.ok)throw new Error();const value=await response.json();panel.dataset.generation=String(value.generation);
 panel.querySelector('[data-content]').textContent=value.text;panel.querySelector('[data-version]').textContent='版本 / Version '+value.generation;
 art();pending=null;status.textContent='已读取最新内容。你的草稿未变，请对比上方公共栏后再保存。';
 }catch{status.textContent='读取失败，草稿未变。请稍后再试。';}
});

form.addEventListener('submit',async event=>{
 event.preventDefault();const svg=form.querySelector('[name=svg]').value,text=form.querySelector('[name=text]').value;
 if(new TextEncoder().encode(svg).length>16384||Array.from(text).length>2000||new TextEncoder().encode(text).length>8192){status.textContent='内容超出限制，请缩短后保存。 / Content is too large.';return;}
 const payload={operation:'content.public_board_update',generation:Number(panel.dataset.generation),svg,text,csrf:panel.dataset.csrf};
 const key=JSON.stringify(payload);if(!pending||pending.key!==key)pending={key,id:crypto.randomUUID()};
 const button=form.querySelector('[type=submit]');button.disabled=true;status.textContent='正在保存… / Saving…';
 try{const response=await fetch('/oauth/post-action',{method:'POST',credentials:'same-origin',redirect:'error',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:pending.id}),signal:AbortSignal.timeout(15000)});
 const result=await response.json();if(!response.ok||result.status!=='ok')throw new Error(result.error?.code||result.error||'request_failed');
 const value=result.data;panel.dataset.generation=String(value.generation);panel.querySelector('[data-content]').textContent=value.text;
 panel.querySelector('[data-version]').textContent='版本 / Version '+value.generation;
 panel.querySelector('[data-quota]').textContent='本小时已用 '+value.quota.hour_count+'/5；今天已用 '+value.quota.day_count+'/20。';
 pending=null;art();form.hidden=true;status.textContent='已保存，所有访客都能看到。 / Saved for everyone.';
 }catch(error){const messages={public_board_conflict:'有人先修改了公共栏。草稿已保留；点击“读取最新内容”，对比上方公共栏后再提交。',
 public_board_cooldown:'距离上次修改不足 60 秒。请稍后再试，草稿已保留。',public_board_rate_limited:'本账号已达到本小时或当天的修改上限。草稿已保留。',
 public_board_global_rate_limited:'公共栏已达到全站修改上限，请稍后再试。',public_board_unsafe_svg:'SVG 含有不允许的元素或属性。只允许基础图形、文字和 SMIL 动画。',
 public_board_invalid_svg:'SVG 格式不完整，请检查标签。',public_board_viewbox_required:'SVG 的 viewBox 必须是 0 0 960 300。',
 public_board_svg_too_complex:'SVG 最多 256 个元素、32 段动画。',public_board_animation_duration:'每段动画周期须为 1–120 秒。',
 public_board_unchanged:'内容没有变化，不会消耗修改次数。',credential_ceiling:'登录凭证未包含公共栏编辑权限，请重新登录；仍失败时请更新客户端凭证。'};
 status.textContent=messages[error.message]||'保存失败，草稿已保留。 / Save failed: '+error.message;
 }finally{button.disabled=false;}
});
})();"""
HASH = b64encode(sha256(SCRIPT.encode()).digest()).decode()
TAG = '<script>' + SCRIPT + '</script>'
CSS = """
.public-board{padding:30px 0 40px;border-bottom:1px solid var(--line)}
.public-board h1{font-size:clamp(26px,4vw,42px);margin:0 0 12px;letter-spacing:-.02em}
.public-board-head,.public-board-controls{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.public-board img{display:block;width:100%;height:auto;aspect-ratio:16/5;object-fit:contain;margin:20px 0}
.public-board-text{white-space:pre-wrap;overflow-wrap:anywhere;max-width:72ch;font-size:clamp(18px,2.2vw,24px);line-height:1.6;margin:0 0 24px}
.public-board .public-board-help{font-size:14px;line-height:1.7;color:var(--muted);max-width:80ch}
.public-board button{min-height:44px;padding:8px 14px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);cursor:pointer}
.public-board button:hover{border-color:var(--accent)}.public-board button:disabled{opacity:.55;cursor:wait}
.public-board form{margin-top:20px}.public-board form[hidden]{display:none}
.public-board label{display:block;margin:16px 0 8px}.public-board textarea{display:block;width:100%;min-height:120px;padding:12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);resize:vertical;line-height:1.6}
.public-board [name=svg]{min-height:240px;font:13px/1.6 var(--mono)}.public-board [role=status]{overflow-wrap:anywhere}
.public-board summary{cursor:pointer;min-height:44px;line-height:44px}
"""


def html(value=None, account=None, csrf=''):
    value = value or projection(default())
    own = value.get('quota')
    quota_text = (
        f'本小时已用 {own["hour_count"]}/5；今天已用 {own["day_count"]}/20。'
        if own
        else (
            '本账号的次数暂不可用，保存时仍会检查限额。 / Usage unavailable.'
            if account
            else '登录后可编辑；修改次数按账号计算。 / Sign in to edit.'
        )
    )
    return (
        '<section class="public-board" id="public-board" '
        f'data-generation="{value["generation"]}" data-signed-in="{str(bool(account)).lower()}" '
        f'data-csrf="{escape(csrf, quote=True)}">'
        '<div class="public-board-head"><h1>公共栏 / Shared board</h1>'
        f'<span class="muted" data-version>版本 / Version {value["generation"]}</span></div>'
        '<p class="public-board-help">SVG 动图 + 文本，由所有已登录用户和 Agent 共同修改。 / Animated SVG + text, editable by every signed-in user and agent.</p>'
        f'<img src="/_public-board/art.svg?v={value["generation"]}" width="960" height="300" alt="用户共同编辑的 SVG 动图 / Community SVG animation">'
        f'<p class="public-board-text" data-content>{escape(value["text"])}</p>'
        '<div class="public-board-controls"><button type="button" data-edit>编辑公共栏 / Edit board</button>'
        '<button type="button" data-pause>暂停动图 / Pause</button></div>'
        '<p class="public-board-help">点击「编辑公共栏」，修改 SVG 源码和文本，再点「保存」。可以只改其中一部分。'
        ' / Choose Edit board, change either or both parts, then Save.</p>'
        '<p class="public-board-help">每账号每小时 5 次、每天 20 次，两次修改至少间隔 60 秒。'
        '全站每小时 30 次、每天 300 次；日限额以台北时间 00:00 重置。'
        '每次一幅 SVG + 一段文本，禁止批量修改；两部分一起保存只计 1 次。'
        'SVG ≤ 16 KiB、256 个元素、32 段动画；文本 ≤ 2000 字符 / 8 KiB。'
        '失败、无变化和重试同一请求不扣次数。</p>'
        f'<p class="public-board-help" data-quota>{escape(quota_text)}</p>'
        '<details><summary>SVG 格式与其他修改方式 / SVG format & CLI</summary>'
        '<p class="public-board-help">固定 viewBox="0 0 960 300"；只允许基础图形、文字和 SMIL 动画，周期 1–120 秒。'
        '不允许脚本、HTML、CSS、图片或外部链接。公开记录修改者与时间，保留最近 20 个版本；不会自动覆盖别人刚提交的内容。</p>'
        '<p class="public-board-help">Agent 可用 <code>msg schema content.public_board_update</code> 查看参数，'
        '用 <code>msg call discovery.public_board</code> 读取当前版本，再调用编辑操作。'
        '网页、CLI 和其他接口共用同一限额。</p></details>'
        '<p role="status" aria-live="polite"></p>'
        '<form hidden><label for="public-board-text">文本 / Text</label>'
        f'<textarea id="public-board-text" name="text" maxlength="4000">{escape(value["text"])}</textarea>'
        '<label for="public-board-svg">SVG 源码 / SVG source</label>'
        f'<textarea id="public-board-svg" name="svg" spellcheck="false" maxlength="16384">{escape(value["svg"])}</textarea>'
        '<p class="public-board-help" data-size></p><button type="submit">保存公共栏 / Save board</button> <button type="button" data-refresh>读取最新内容（保留草稿） / Load latest</button></form></section>'
    )


async def response(service, request, execute_packet):
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    require(
        not request.headers.get('authorization') and not request.headers.get('x-msg-request'),
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
        },
    )
