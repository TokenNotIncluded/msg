"""Small, CSP-pinned post controls; writes use the current browser grant."""

from base64 import b64encode
from hashlib import sha256
from html import escape
from urllib.parse import urlencode

POST_ACTIONS_CSS = """
.post-actions { margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--line); }
.post-action-row { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.post-actions button { display: inline-flex; align-items: center; justify-content: center;
  gap: 8px; min-height: 44px; padding: 8px 12px; border: 1px solid transparent;
  border-radius: 2px; background: transparent; color: var(--fg); font: inherit;
  font-size: 14px; line-height: 1.4; cursor: pointer; }
.post-actions button:hover:not(:disabled), .post-proof-claims summary:hover { background: var(--panel); }
.post-actions .post-comment { background: var(--fg); color: var(--bg); }
.post-actions .post-comment:hover:not(:disabled) { background: var(--fg); text-decoration: underline; }
.post-actions button[aria-pressed=true] { color: var(--accent); }
.post-actions button:disabled { color: var(--muted); cursor: not-allowed; }
.post-actions .post-comment:disabled { background: var(--panel); }
.post-action-row [data-action=follow] { margin-inline-start: auto; color: var(--muted); }
.post-actions svg { flex: none; width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.6; }
.post-action-meta { display: flex; flex-wrap: wrap; align-items: start; gap: 0 24px; margin-top: 8px; }
.post-proof-claims { flex: 1 1 auto; min-width: 110px; }
.post-proof-claims[open] { flex-basis: 100%; }
.post-proof-claims summary { display: flex; align-items: center; gap: 8px; min-height: 44px;
  width: fit-content; max-width: 100%; padding: 8px 12px; list-style: none;
  color: var(--muted); font-size: 13px; cursor: pointer; }
.post-proof-claims summary::-webkit-details-marker { display: none; }
.post-proof-claims summary::before { content: '[+]'; font: 12px/1 var(--mono); }
.post-proof-claims[open] summary::before { content: '[-]'; }
.post-proof-options { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
  gap: 4px 16px; margin-block: 8px 16px; }
.post-proof-options button { justify-content: start; width: 100%; text-align: start; }
.post-proof-options code { flex: none; width: 8ch; font: 12px/1.5 var(--mono); }
.post-proof-options span { font-size: 13px; }
.post-proof-options small { margin-inline-start: auto; font: 12px/1.5 var(--mono); font-variant-numeric: tabular-nums; }
.post-action-links { display: flex; flex-wrap: wrap; gap: 0 16px; }
.post-action-links a { display: inline-flex; align-items: center; min-height: 44px;
  color: var(--muted); font-size: 12px; text-decoration: none; }
.post-action-links a:hover { color: var(--fg); text-decoration: underline; }
.post-actions p { margin: 8px 0; font-size: 13px; color: var(--muted); }
.post-actions [role=status]:empty { display: none; }
.post-actions [role=status] { overflow-wrap: anywhere; }
.post-actions [role=status] a { display: inline-flex; align-items: center; min-height: 44px; margin-inline-start: 8px; }
.post-actions form { margin-top: 16px; }
.post-actions label { font-size: 14px; }
.post-compose-actions { display: flex; align-items: center; gap: 8px; }
.post-compose-actions [type=submit] { background: var(--fg); color: var(--bg); }
.post-actions textarea { display: block; box-sizing: border-box; width: 100%; min-height: 120px;
  margin-block: 10px; padding: 12px; border: 1px solid var(--line); border-radius: 2px;
  background: var(--bg); color: var(--fg); font: inherit; resize: vertical; }
.post-actions :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (max-width: 480px) {
  .post-action-row { gap: 4px; }
  .post-action-row button { padding-inline: 8px; font-size: 13px; }
  .post-action-row [data-action=follow] svg { display: none; }
  .post-action-links { gap: 0 20px; }
  .post-proof-options { grid-template-columns: 1fr; }
}
.post-actions [hidden] { display: none; }
"""

POST_ACTIONS_SCRIPT = r"""(() => {
  const panel = document.getElementById('post-actions');
  if (!panel) return;
  const text = (en, zh) => globalThis.msgText?.(en, zh) ?? (document.documentElement.lang.startsWith('zh') ? zh : en);
  const status = panel.querySelector('[role=status]');
  const buttons = [...panel.querySelectorAll('[data-action]')];
  let ready = false, writing = false, countsAvailable = false, proofsKnown = false, proofWritten = false;
  const field = panel.querySelector('textarea');
  const closeCompose = panel.querySelector('[data-compose-close]');
  const availability = () => {
    for (const button of buttons) button.disabled = writing || (!ready && ['bookmark','follow'].includes(button.dataset.action));
    field.disabled = writing;
    closeCompose.disabled = writing;
    panel.querySelector('[type=submit]').disabled = writing;
  };
  availability();
  let state = {bookmarked:false, following:false, proofs:{}, my_proofs:[]};
  let compose = 'comment';
  const labels = {
    ACK: [['Read','我读过'],['Read','我读过']],
    USED: [['Used','我使用过'],['Used','我使用过']],
    VERIFIED: [['Verified','我验证过'],['Verified','我验证过']],
    SOLVED: [['Solved my problem','解决了我的问题'],['Solved my problem','解决了我的问题']],
    THANKS: [['Thank you','感谢'],['Thank you','感谢']],
    fork: [['Fork','分叉'],['Fork','分叉']],
    bookmark: [['Save','收藏'],['Saved','已收藏']],
    follow: [['Follow','关注'],['Following','已关注']],
    comment: [['Comment','评论'],['Comment','评论']]
  };
  const update = () => {
    for (const button of buttons) {
      const kind = button.dataset.action;
      const enabled = state.my_proofs.includes(kind) ? true : kind === 'bookmark' ? state.bookmarked : kind === 'follow' ? state.following : false;
      if(!['comment','fork'].includes(kind)) {
        if(['bookmark','follow'].includes(kind) ? ready : proofsKnown) button.setAttribute('aria-pressed', String(enabled));
        else button.removeAttribute('aria-pressed');
      }
      const pair = labels[kind][enabled ? 1 : 0];
      button.querySelector('span').textContent = text(...pair);
      if(button.querySelector('small')) button.querySelector('small').textContent = countsAvailable ? String(state.proofs[kind] || 0) : '—';
    }
    panel.querySelector('[data-copy=collection]').textContent = text('My saved posts','我的收藏');
    panel.querySelector('[data-copy=proofs]').textContent = text('Proof records','证明记录');
    panel.querySelector('[data-copy=forks]').textContent = text('Branches','查看分叉');
    panel.querySelector('[data-copy=claims]').textContent = text('Proof claims','证明声明');
    panel.querySelector('[data-copy=claim-note]').textContent = text('Personal claims about this revision, not platform endorsements.','对这个版本的个人声明，不代表平台背书。');
    closeCompose.textContent = text('Close','收起');
    panel.querySelector('textarea').maxLength = ['comment','fork'].includes(compose) ? 20000 : 4096;
    panel.querySelector('label').textContent = compose === 'fork' ? text('New branch content','新分支内容') : labels[compose] && compose !== 'comment' ? text('Optional evidence / usage / result (this is your claim)','可选：使用过程、验证依据或结果（这是你的声明）') : text('Your comment','你的评论');
    panel.querySelector('[type=submit]').textContent = compose === 'fork' ? text('Create branch','创建分支') : compose !== 'comment' ? text('Record claim','提交证明声明') : text('Send comment','发表评论');
  };
  new MutationObserver(update).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
  update();
  const recover = error => {
    const code = error.message;
    const auth = ['invalid_grant','authentication_required','credential_not_found'].includes(code);
    status.textContent = code === 'credential_ceiling' ?
      text('Your current authorization does not include this operation. Your draft is kept; authorization that includes it is required.','当前授权未包含此操作。草稿已保留，需要包含该操作的授权。') : auth ?
      text('Your sign-in has expired or is unavailable. Your draft stays on this page; copy it before signing in again. ','登录已失效或不可用。草稿仍在此页，重新登录前先复制保存。') :
      text('Could not complete the action. Your draft is kept. ','操作未完成，草稿已保留。') + code;
    if(auth) { const link=document.createElement('a');link.href='/login';link.textContent=text('Sign in again','重新登录');status.append(link); }
  };
  const send = async (operation, extra, requestId) => {
    const response = await fetch('/oauth/post-action', {
      method:'POST', credentials:'same-origin',redirect:'error',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({csrf:panel.dataset.csrf,id:panel.dataset.id,operation,request_id:requestId,...extra}),
      signal:AbortSignal.timeout(15000)
    });
    const data=await response.json();
    if(!response.ok || data.status !== 'ok') throw new Error(typeof data.error === 'string' ? data.error : data.error?.code || 'request_failed');
    return data;
  };
  const signedIn = () => { if(panel.dataset.signedIn !== 'true') { location.assign('/login'); return false; } return true; };
  for (const button of buttons) button.addEventListener('click', async () => {
    if(writing) return;
    if(!signedIn()) return;
    const kind=button.dataset.action;
    if(['comment','fork','ACK','USED','VERIFIED','SOLVED','THANKS'].includes(kind)) { compose=kind;panel.querySelector('textarea').required=['comment','fork'].includes(kind);panel.querySelector('form').hidden=false;update();panel.querySelector('textarea').focus();return; }
    if(!ready) return;
    const enabled=kind==='bookmark'?state.bookmarked:state.following;
    const operation=(kind==='follow'?'communication.':'discussion.')+(enabled?'un':'')+kind;
    writing=true;availability();status.textContent=text('Saving…','正在保存…');
    const key=operation+':'+(kind==='follow'?panel.dataset.author:panel.dataset.id);
    if(button.dataset.pending !== key) { button.dataset.pending=key;button.dataset.requestId=crypto.randomUUID(); }
    try {
      const result=await send(operation,kind==='follow'?{id:panel.dataset.author}:{},button.dataset.requestId);
      state={...state,...result.data};delete button.dataset.pending;update();
      status.textContent=text('Saved.','已保存。');
    } catch(error) { recover(error); }
    finally { writing=false;availability(); }
  });
  closeCompose.addEventListener('click', () => { if(!writing) panel.querySelector('form').hidden=true; });
  let pending;
  panel.querySelector('form').addEventListener('submit',async event => {
    event.preventDefault();if(writing || !signedIn()) return;
    const kind=compose, body=field.value.trim();if(!body && ['comment','fork'].includes(kind)) {field.focus();return;}
    writing=true;availability();
    if(!pending || pending.body!==body || pending.kind!==kind) pending={body,kind,id:crypto.randomUUID()};
    status.textContent=text('Sending…','正在发送…');
    try {
      const isProof=!['comment','fork'].includes(kind);
      const operation=isProof?'discussion.prove':kind==='fork'?'discussion.fork':'discussion.reply';
      const result=await send(operation,isProof?{kind,note:body,revision:panel.dataset.revision}:{body,revision:panel.dataset.revision},pending.id);
      if(isProof) { state={...state,...result.data};proofWritten=true;countsAvailable=proofsKnown=true;update();field.value='';pending=null;panel.querySelector('form').hidden=true;status.textContent=text('Claim recorded for this revision.','已记录对此版本的证明声明。');return; }
      field.value='';pending=null;status.textContent=kind==='fork'?text('Branch created. ','分支已创建。'):text('Comment posted. ','评论已发表。');
      const link=document.createElement('a');link.href='/_id/'+encodeURIComponent(result.resources[0].id);
      link.textContent=kind==='fork'?text('View branch','查看分支'):text('View comment','查看评论');status.append(link);
      if(kind==='comment') window.dispatchEvent(new CustomEvent('msg:reply-posted',{detail:{parent:panel.dataset.id}}));
    } catch(error) {recover(error);} finally {writing=false;availability();}
  });
  fetch('/_post/state?id='+encodeURIComponent(panel.dataset.id)+'&revision='+encodeURIComponent(panel.dataset.revision),{credentials:'same-origin',headers:{Accept:'application/json'},redirect:'error',signal:AbortSignal.timeout(15000)})
    .then(async response=>{
      const result=await response.json();
      if(!response.ok || result.status!=='ok') throw new Error(typeof result.error==='string'?result.error:result.error?.code || 'request_failed');
      const data={...result.data};
      // This initial read may predate an explicit claim made while it was pending.
      if(proofWritten) { delete data.proofs;delete data.my_proofs; }
      state={...state,...data};countsAvailable=true;ready=result.data.personal_state_available!==false;
      proofsKnown=proofWritten || ready;availability();update();
      if(!ready && !proofWritten) status.textContent=text('Showing public proof counts. Saved and followed status are unavailable for your current authorization.','仅显示公开证明次数；当前授权无法读取你的收藏和关注状态。');
    })
    .catch(error=>{if(!proofWritten) recover(error);});
})();"""

POST_ACTIONS_HASH = b64encode(sha256(POST_ACTIONS_SCRIPT.encode()).digest()).decode()


def post_actions_html(resource, account, csrf):
    author = resource.get('links', {}).get('a', {}).get('ref', {}).get('id')
    attrs = {
        'id': resource['id'],
        'revision': resource['revision'],
        'author': author or '',
        'csrf': csrf,
        'signed-in': str(bool(account)).lower(),
    }
    data = ' '.join(
        f'data-{key}="{escape(str(value), quote=True)}"' for key, value in attrs.items()
    )
    icons = {
        'bookmark': '<path d="M6 3h12v18l-6-4-6 4Z"/>',
        'follow': '<circle cx="9" cy="7" r="4"/><path d="M2 21v-2a7 7 0 0 1 14 0v2m3-12v6m-3-3h6"/>',
        'comment': '<path d="M3 3h18v14H8l-5 4Z"/>',
        'fork': '<circle cx="6" cy="4" r="2"/><circle cx="18" cy="4" r="2"/><circle cx="6" cy="20" r="2"/><path d="M6 6v12m12-12v2a4 4 0 0 1-4 4H6"/>',
    }
    buttons, proofs = '', ''
    for kind, label in [
        ('comment', 'Comment'),
        ('bookmark', 'Save'),
        ('fork', 'Fork'),
        ('follow', 'Follow'),
        ('ACK', 'Read'),
        ('USED', 'Used'),
        ('VERIFIED', 'Verified'),
        ('SOLVED', 'Solved my problem'),
        ('THANKS', 'Thank you'),
    ]:
        if kind == 'follow' and (not author or account and account['id'] == author):
            continue
        is_proof = kind in {'ACK', 'USED', 'VERIFIED', 'SOLVED', 'THANKS'}
        button = (
            f'<button type="button" data-action="{kind}"'
            + (' class="post-comment"' if kind == 'comment' else '')
            + (' aria-pressed="false"' if kind not in {'comment', 'fork'} else '')
            + '>'
            + (
                f'<code>{kind}</code>'
                if is_proof
                else f'<svg viewBox="0 0 24 24" aria-hidden="true">{icons[kind]}</svg>'
            )
            + f'<span>{label}</span>'
            + ('<small>—</small>' if is_proof else '')
            + '</button>'
        )
        if is_proof:
            proofs += button
        else:
            buttons += button
    proof_url = '/_post/proofs?' + urlencode({
        'id': resource['id'],
        'revision': resource['revision'],
    })
    forks_url = '/_post/forks?' + urlencode({'id': resource['id']})
    return (
        f'<section class="post-actions" id="post-actions" {data} aria-label="Post actions">'
        + '<div class="post-action-row" role="group" aria-label="Post actions">'
        + buttons
        + '</div><div class="post-action-meta"><details class="post-proof-claims">'
        + '<summary><span data-copy="claims">Proof claims</span></summary>'
        + '<p data-copy="claim-note">Personal claims about this revision, not platform endorsements.</p>'
        + '<div class="post-proof-options">'
        + proofs
        + '</div></details><nav class="post-action-links" aria-label="Related records">'
        + '<a href="/bookmarks" data-copy="collection">My saved posts</a>'
        + f'<a href="{escape(proof_url, quote=True)}" data-copy="proofs">Proof records</a>'
        + f'<a href="{escape(forks_url, quote=True)}" data-copy="forks">Branches</a></nav></div>'
        + '<p role="status" aria-live="polite"></p>'
        + '<form hidden><label for="post-comment">Your comment</label>'
        + '<textarea id="post-comment" maxlength="20000" required></textarea>'
        + '<div class="post-compose-actions"><button type="submit">Send comment</button>'
        + '<button type="button" data-compose-close>Close</button></div></form></section>'
    )
