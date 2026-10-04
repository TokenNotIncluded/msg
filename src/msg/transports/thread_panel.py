"""讨论入口留在帖子里；统计不完整时不显示假的零回复。"""

import hashlib
from base64 import b64encode
from html import escape
from urllib.parse import quote, urlencode

from msg.core.identifiers import hex_id

CSS = """
.reply-link { display: inline-flex; align-items: center; gap: 8px; min-height: 44px;
  color: var(--fg); text-decoration: none; font-variant-numeric: tabular-nums; }
.reply-link:hover { text-decoration: underline; }
.post-engagement { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 20px; }
.discussion-panel { margin-block: 64px 32px; padding-block: 0 24px; }
.discussion-heading { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; }
.prose .discussion-heading h2 { margin: 0; font-size: 1.375rem; font-weight: 550; }
.discussion-count, .discussion-note { color: var(--muted); font-size: .875rem; }
.discussion-controls { display: flex; flex-wrap: wrap; gap: 8px 20px; margin-block: 24px 28px; }
.discussion-controls button, .discussion-controls a { min-height: 44px; display: inline-flex;
  align-items: center; justify-content: center; padding: 8px 4px; border: 0;
  background: transparent; color: var(--muted); font: inherit; font-size: .875rem;
  cursor: pointer; text-decoration: none; border-radius: 2px; }
.discussion-controls button:hover, .discussion-controls a:hover { color: var(--fg);
  text-decoration: underline; text-underline-offset: .25em; }
.discussion-controls button[data-thread-expand] { padding-inline: 16px;
  background: var(--fg); color: var(--bg); }
.discussion-controls button[data-thread-expand]:hover { background: var(--muted);
  color: var(--bg); text-decoration: none; }
.discussion-controls button:disabled { color: var(--muted); cursor: wait; }
.discussion-controls button[data-thread-expand]:disabled { background: var(--panel);
  color: var(--muted); }
.discussion-controls :focus-visible, .reply-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.discussion-preview { list-style: none; padding: 0; margin: 24px 0; }
.discussion-preview li { padding-block: 18px; }
.discussion-preview li + li { margin-block-start: 12px; }
.discussion-preview p { margin: 8px 0; }
.discussion-preview a { display: inline-flex; align-items: center; min-height: 44px; }
.discussion-status { color: var(--muted); min-height: 1.5em; font-size: .875rem; }
.discussion-panel .thread-discussion { margin-block: 24px; }
.discussion-panel [aria-busy="true"] { opacity: .75; }
button.thread-control { padding-inline: 4px; font: inherit; font-size: .875rem;
  color: var(--fg); background: transparent; border: 0; cursor: pointer;
  text-decoration: underline; text-underline-offset: .25em; }
@media (max-width: 640px) {
  .discussion-panel { margin-top: 48px; }
  .discussion-controls { column-gap: 16px; }
}
@media (prefers-reduced-motion: no-preference) {
  .discussion-controls button { transition: background-color 120ms ease-out; }
}
"""

SCRIPT = r"""(()=>{
  const mainMore=container=>container.querySelector(':scope > [data-thread-fragment] > .thread-more');
  const read=async(url)=>{
    const response=await fetch(url,{credentials:'same-origin',redirect:'error',headers:{Accept:'text/html'},signal:AbortSignal.timeout(15000)});
    if(!response.ok||!(response.headers.get('content-type')||'').includes('text/html'))throw new Error('read_failed');
    const html=await response.text();if(html.length>1048576)throw new Error('too_large');
    const template=document.createElement('template');template.innerHTML=html;
    const fragment=template.content.querySelector('[data-thread-fragment]');
    if(!fragment||fragment.querySelector('script,iframe,object,embed'))throw new Error('invalid_fragment');
    return fragment;
  };
  const loadBranch=async(branch,url,append=false)=>{
    if(branch.dataset.loading)return;
    const content=branch.querySelector(':scope > [data-thread-branch-content]');if(!content)return;
    branch.dataset.loading='true';content.setAttribute('aria-busy','true');
    const pending=content.querySelector('.thread-branch-pending');if(pending)pending.textContent='正在读取子回复…';
    try{
      const fragment=await read(url||('/_post/thread-fragment?'+new URLSearchParams({id:branch.dataset.postId,limit:8})));
      fragment.removeAttribute('id');fragment.removeAttribute('aria-labelledby');
      fragment.querySelector('.thread-heading')?.remove();
      const depth=Number(branch.closest('.thread-node')?.dataset.depth||0)+1;
      fragment.querySelectorAll('.thread-node').forEach(node=>node.dataset.depth=String(depth+Number(node.dataset.depth||0)));
      fragment.querySelectorAll('[id]').forEach(element=>{if(document.getElementById(element.id))element.removeAttribute('id');});
      if(append){mainMore(content)?.remove();content.append(fragment);}
      else content.replaceChildren(fragment);
      if(fragment.dataset.replyLabel)branch.querySelector(':scope > summary .thread-reply-count').textContent=fragment.dataset.replyLabel;
      branch.dataset.loaded='true';
    }catch{
      if(pending)pending.textContent='子回复暂时无法读取。';
      let retry=content.querySelector('[data-branch-retry]');
      if(!retry){retry=document.createElement('button');retry.type='button';retry.className='thread-control';retry.dataset.branchRetry='';content.append(retry);}
      retry.textContent='读取失败，点击重试';
    }finally{delete branch.dataset.loading;content.removeAttribute('aria-busy');}
  };
for(const panel of document.querySelectorAll('[data-thread-panel]')){
  const target=panel.querySelector('[data-thread-content]'),status=panel.querySelector('[data-thread-status]');
  const expand=panel.querySelector('[data-thread-expand]'),refresh=panel.querySelector('[data-thread-refresh]');
  let loaded=false,loading=false,changes=0,loadedChanges=-1;
  const label=value=>{
    if(!value)return;
    panel.querySelector('.discussion-count').textContent=value;
    document.querySelectorAll('.page-document .reply-link').forEach(link=>link.textContent=value);
  };
  const busy=value=>{
    loading=value;expand.disabled=refresh.disabled=value;
    if(value)target.setAttribute('aria-busy','true');else target.removeAttribute('aria-busy');
  };
  const counts=async()=>{
    if(loading)return;busy(true);status.textContent='正在刷新数量…';
    try{
      const response=await fetch(panel.dataset.statusUrl,{credentials:'same-origin',redirect:'error',headers:{Accept:'application/json'},signal:AbortSignal.timeout(15000)});
      if(!response.ok||!(response.headers.get('content-type')||'').includes('application/json'))throw new Error('read_failed');
      const text=await response.text();if(text.length>65536)throw new Error('too_large');
      const data=JSON.parse(text).data;
      if(data?.id!==panel.dataset.postId||typeof data.reply_label!=='string')throw new Error('invalid_status');
      label(data.reply_label);status.textContent='';
    }catch{status.textContent='数量暂时无法刷新，可以重试。';}
    finally{busy(false);}
  };
  const load=async(url=panel.dataset.threadUrl,append=false)=>{
    if(loading)return;busy(true);const version=changes;
    status.textContent='正在读取讨论…';
    try{
      const fragment=await read(url);
      if(append){
        const nextLatest=fragment.querySelector('.thread-actions a[href^="#reply-"]');
        const currentLatest=target.querySelector('.thread-actions a[href^="#reply-"]');
        if(nextLatest&&currentLatest)currentLatest.setAttribute('href',nextLatest.getAttribute('href'));
        else if(nextLatest)target.querySelector('.thread-actions')?.append(nextLatest.cloneNode(true));
        mainMore(target)?.remove();
        fragment.removeAttribute('id');fragment.removeAttribute('aria-labelledby');
        fragment.querySelector('.thread-heading')?.remove();
        fragment.querySelectorAll('[id]').forEach(element=>{if(document.getElementById(element.id))element.removeAttribute('id');});
        target.append(fragment);
      }else target.replaceChildren(fragment);
      target.hidden=false;loaded=true;loadedChanges=version;label(fragment.dataset.replyLabel);
      const range=target.querySelector('.thread-range');
      if(range)range.textContent='已展开 '+target.querySelectorAll('.thread-node:not(.thread-root)').length+' 条回复'+(mainMore(target)?' · 还有更多':'');
      expand.textContent='收起讨论';expand.setAttribute('aria-expanded','true');status.textContent='';
    }catch{
      status.textContent='讨论暂时无法读取。现有内容保留，可以重试。';
      expand.textContent=target.hidden?'重试读取讨论':'收起讨论';
    }finally{busy(false);}
  };
  expand.addEventListener('click',()=>{
    if(target.hidden&&(!loaded||loadedChanges!==changes)){load();return;}
    target.hidden=!target.hidden;expand.textContent=target.hidden?'展开讨论':'收起讨论';
    expand.setAttribute('aria-expanded',String(!target.hidden));
  });
  refresh.addEventListener('click',()=>{changes++;if(target.hidden)counts();else load();});
  target.addEventListener('click',event=>{
    const latest=event.target.closest('.thread-actions a[href^="#reply-"]');
    if(latest){
      const node=document.getElementById(new URL(latest.href,location.href).hash.slice(1));
      if(node&&target.contains(node))for(let branch=node.parentElement.closest('details');branch&&target.contains(branch);branch=branch.parentElement.closest('details'))branch.open=true;
    }
    const link=event.target.closest('.thread-more a');if(!link)return;
    const url=new URL(link.href,location.href);
    if(url.origin!==location.origin||url.pathname!=='/_post/thread-fragment')return;
    if(link.closest('[data-thread-branch]'))return;
    event.preventDefault();if(!loading)load(url.href,true);
  });
  window.addEventListener('msg:reply-posted',event=>{
    if(event.detail?.parent!==panel.dataset.postId)return;
    changes++;if(target.hidden)counts();else load();
  });
}
document.addEventListener('toggle',event=>{
  const branch=event.target;
  if(!branch.matches('[data-thread-branch]')||!branch.closest('[data-thread-fragment]')||!branch.open||branch.dataset.loaded)return;
  const content=branch.querySelector(':scope > [data-thread-branch-content]');
  if(content?.querySelector('.thread-node')){branch.dataset.loaded='true';return;}
  loadBranch(branch);
},true);
document.addEventListener('click',event=>{
  const retry=event.target.closest('[data-branch-retry]');
  if(retry&&retry.closest('[data-thread-fragment]'))loadBranch(retry.closest('[data-thread-branch]'));
  const link=event.target.closest('[data-thread-branch] .thread-more a');
  if(link){
    const url=new URL(link.href,location.href);
    if(url.origin===location.origin&&url.pathname==='/_post/thread-fragment'){
      event.preventDefault();loadBranch(link.closest('[data-thread-branch]'),url.href,true);
    }
  }
  const latest=event.target.closest('[data-thread-fragment] .thread-actions a[href^="#reply-"]');
  if(latest&&!latest.closest('[data-thread-panel]')){
    const node=document.getElementById(new URL(latest.href,location.href).hash.slice(1));
    if(node)for(let branch=node.parentElement.closest('details');branch;branch=branch.parentElement.closest('details'))branch.open=true;
  }
});
})();"""
HASH = b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()


def reply_label(status):
    if status is None:
        return '回复状态暂不可用'
    count = int(status.get('reply_count', 0))
    if not status.get('count_complete', False):
        return ('公开 ' if status.get('public_only') else '') + (
            f'至少 {count} 条回复' if count else '回复尚未完整加载'
        )
    return ('公开 ' if status.get('public_only') else '') + f'{count} 条回复'


def reply_link(status, path):
    href = quote(path, safe='/@*') + '#discussion'
    return (
        '<a class="reply-link" href="'
        + escape(href, quote=True)
        + '">'
        + escape(reply_label(status))
        + '</a>'
    )


def discussion_note(status):
    if status and status.get('thread_count') is not None:
        total = int(status['thread_count'])
        scope = '' if status.get('thread_complete', False) else '至少 '
        return f'这场讨论共有 {scope}{total} 条可读回复。'
    return '直接回复此帖。展开可查看完整讨论的分支。'


def discussion_panel_html(resource, status):
    rid = resource['id']
    url = '/_post/thread-fragment?' + urlencode({'id': rid, 'limit': 8})
    status_url = '/_post/reply-status?' + urlencode({'id': rid})
    fallback = '/*' + hex_id(rid) + '/thread'
    return (
        '<section class="discussion-panel" id="discussion" data-thread-panel data-post-id="'
        + escape(rid, quote=True)
        + '" data-thread-url="'
        + escape(url, quote=True)
        + '" data-status-url="'
        + escape(status_url, quote=True)
        + '"><div class="discussion-heading"><h2>讨论</h2><span class="discussion-count">'
        + escape(reply_label(status))
        + '</span></div><div class="discussion-controls"><button type="button" data-thread-expand aria-expanded="false" aria-controls="discussion-content">展开讨论</button>'
        + '<button type="button" data-thread-refresh>刷新数量</button><noscript><a href="'
        + fallback
        + '">打开完整讨论</a></noscript></div><div id="discussion-content" data-thread-content hidden></div>'
        + '<p class="discussion-status" data-thread-status role="status" aria-live="polite"></p></section>'
    )
