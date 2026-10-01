"""Small, CSP-pinned post controls; writes use the current browser grant."""

from base64 import b64encode
from hashlib import sha256
from html import escape
from urllib.parse import urlencode

POST_ACTIONS_CSS = """
.post-actions { margin-top: 32px; padding-top: 20px; border-top: 1px solid var(--line); }
.post-action-row { display: flex; flex-wrap: wrap; gap: 8px; }
.post-actions button, .post-actions .bookmarks-link { display: inline-flex; align-items: center;
  gap: 7px; min-height: 44px; padding: 8px 12px; border: 1px solid var(--line);
  border-radius: 8px; background: transparent; color: var(--fg); font: inherit;
  font-size: 14px; text-decoration: none; cursor: pointer; }
.post-actions button:hover, .post-actions .bookmarks-link:hover { background: var(--panel); }
.post-actions button[aria-pressed=true] { color: var(--accent); border-color: var(--accent); }
.post-actions button:disabled { opacity: .6; cursor: wait; }
.post-actions svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.6; }
.post-actions p { font-size: 14px; color: var(--muted); }
.post-actions textarea { display: block; box-sizing: border-box; width: 100%; min-height: 120px;
  margin-block: 10px; padding: 12px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg); color: var(--fg); font: inherit; resize: vertical; }
.post-actions [hidden] { display: none; }
"""

POST_ACTIONS_SCRIPT = r"""(() => {
  const panel = document.getElementById('post-actions');
  if (!panel) return;
  const text = (en, zh) => document.documentElement.lang.startsWith('zh') ? zh : en;
  const status = panel.querySelector('[role=status]');
  const buttons = [...panel.querySelectorAll('[data-action]')];
  let ready = false;
  for (const button of buttons) if(button.dataset.action !== 'comment') button.disabled=true;
  let state = {bookmarked:false, following:false, proofs:{}, my_proofs:[]};
  let compose = 'comment';
  const labels = {
    ACK: [['ACK · Read','ACK · 我读过'],['ACK · Read','ACK · 我读过']],
    USED: [['USED · Used','USED · 我实际使用过'],['USED · Used','USED · 我实际使用过']],
    VERIFIED: [['VERIFIED · Verified','VERIFIED · 我验证过'],['VERIFIED · Verified','VERIFIED · 我验证过']],
    SOLVED: [['SOLVED · Solved my problem','SOLVED · 解决了我的问题'],['SOLVED · Solved my problem','SOLVED · 解决了我的问题']],
    THANKS: [['THANKS · Thank you','THANKS · 感谢'],['THANKS · Thank you','THANKS · 感谢']],
    fork: [['Fork thread','分叉帖子'],['Fork thread','分叉帖子']],
    bookmark: [['Save','收藏'],['Saved','已收藏']],
    follow: [['Follow author','关注作者'],['Following','已关注']],
    comment: [['Comment','评论'],['Comment','评论']]
  };
  const update = () => {
    for (const button of buttons) {
      const kind = button.dataset.action;
      const enabled = state.my_proofs.includes(kind) ? true : kind === 'bookmark' ? state.bookmarked : kind === 'follow' ? state.following : false;
      if(!['comment','fork'].includes(kind)) button.setAttribute('aria-pressed', String(enabled));
      const pair = labels[kind][enabled ? 1 : 0];
      button.querySelector('span').textContent = text(...pair);
      if(button.querySelector('small')) button.querySelector('small').textContent = String(state.proofs[kind] || 0);
    }
    panel.querySelector('[data-copy=collection]').textContent = text('My saved posts','我的收藏');
    panel.querySelector('[data-copy=proofs]').textContent = text('View proof records','查看证明记录');
    panel.querySelector('[data-copy=forks]').textContent = text('View branches','查看分叉');
    panel.querySelector('textarea').maxLength = ['comment','fork'].includes(compose) ? 20000 : 4096;
    panel.querySelector('label').textContent = compose === 'fork' ? text('New branch content','新分支内容') : labels[compose] && compose !== 'comment' ? text('Optional evidence / usage / result (this is your claim)','可选：使用过程、验证依据或结果（这是你的声明）') : text('Your comment','你的评论');
    panel.querySelector('[type=submit]').textContent = compose === 'fork' ? text('Create branch','创建分支') : compose !== 'comment' ? text('Record claim','提交证明声明') : text('Send comment','发表评论');
  };
  new MutationObserver(update).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
  update();
  const recover = error => {
    const code = error.message;
    const auth = ['credential_ceiling','invalid_grant','authentication_required','credential_not_found'].includes(code);
    status.textContent = auth ? text('This session needs a new approval to interact. ','当前会话需要重新授权才能操作。') :
      text('Could not complete the action. Your comment is kept. ','操作未完成，评论内容已保留。') + code;
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
    if(!signedIn()) return;
    const kind=button.dataset.action;
    if(['comment','fork','ACK','USED','VERIFIED','SOLVED','THANKS'].includes(kind)) { compose=kind;panel.querySelector('textarea').required=['comment','fork'].includes(kind);panel.querySelector('form').hidden=false;update();panel.querySelector('textarea').focus();return; }
    if(!ready) return;
    const enabled=kind==='bookmark'?state.bookmarked:state.following;
    const operation=(kind==='follow'?'communication.':'discussion.')+(enabled?'un':'')+kind;
    button.disabled=true;status.textContent=text('Saving…','正在保存…');
    const key=operation+':'+(kind==='follow'?panel.dataset.author:panel.dataset.id);
    if(button.dataset.pending !== key) { button.dataset.pending=key;button.dataset.requestId=crypto.randomUUID(); }
    try {
      const result=await send(operation,kind==='follow'?{id:panel.dataset.author}:{},button.dataset.requestId);
      state={...state,...result.data};delete button.dataset.pending;update();
      status.textContent=text('Saved.','已保存。');
    } catch(error) { recover(error); }
    finally { button.disabled=false; }
  });
  let pending;
  panel.querySelector('form').addEventListener('submit',async event => {
    event.preventDefault();if(!signedIn()) return;
    const field=panel.querySelector('textarea'), body=field.value.trim();if(!body && ['comment','fork'].includes(compose)) {field.focus();return;}
    const button=panel.querySelector('[type=submit]');button.disabled=true;
    if(!pending || pending.body!==body || pending.kind!==compose) pending={body,kind:compose,id:crypto.randomUUID()};
    status.textContent=text('Sending…','正在发送…');
    try {
      const isProof=!['comment','fork'].includes(compose);
      const operation=isProof?'discussion.prove':compose==='fork'?'discussion.fork':'discussion.reply';
      const result=await send(operation,isProof?{kind:compose,note:body,revision:panel.dataset.revision}:{body,revision:panel.dataset.revision},pending.id);
      if(isProof) { state={...state,...result.data};update();field.value='';pending=null;panel.querySelector('form').hidden=true;status.textContent=text('Claim recorded for this revision.','已记录对此版本的证明声明。');return; }
      field.value='';pending=null;status.textContent=compose==='fork'?text('Branch created. ','分支已创建。'):text('Comment posted. ','评论已发表。');
      const link=document.createElement('a');link.href='/_id/'+encodeURIComponent(result.resources[0].id);
      link.textContent=compose==='fork'?text('View branch','查看分支'):text('View comment','查看评论');status.append(link);
    } catch(error) {recover(error);} finally {button.disabled=false;}
  });
  fetch('/_post/state?id='+encodeURIComponent(panel.dataset.id)+'&revision='+encodeURIComponent(panel.dataset.revision),{credentials:'same-origin',headers:{Accept:'application/json'},redirect:'error',signal:AbortSignal.timeout(15000)})
    .then(async response=>{const result=await response.json();if(!response.ok || result.status!=='ok') throw new Error(result.error?.code || 'request_failed');state={...state,...result.data};ready=true;for(const button of buttons) button.disabled=false;update();})
    .catch(recover);
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
    }
    buttons = ''
    for kind, label in [
        *[
            (kind, label)
            for kind, label in [
                ('ACK', 'ACK · Read'),
                ('USED', 'USED · Used'),
                ('VERIFIED', 'VERIFIED · Verified'),
                ('SOLVED', 'SOLVED · Solved my problem'),
                ('THANKS', 'THANKS · Thank you'),
            ]
        ],
        ('fork', 'Fork thread'),
        ('bookmark', 'Save'),
        ('follow', 'Follow author'),
        ('comment', 'Comment'),
    ]:
        if kind == 'follow' and (not author or account and account['id'] == author):
            continue
        buttons += (
            f'<button type="button" data-action="{kind}"'
            + (' aria-pressed="false"' if kind not in {'comment', 'fork'} else '')
            + f'><svg viewBox="0 0 24 24" aria-hidden="true">{icons.get(kind, icons["comment"])}</svg>'
            + f'<span>{label}</span>'
            + ('<small></small>' if kind in {'ACK', 'USED', 'VERIFIED', 'SOLVED', 'THANKS'} else '')
            + '</button>'
        )
    proof_url = '/_post/proofs?' + urlencode({
        'id': resource['id'],
        'revision': resource['revision'],
    })
    forks_url = '/_post/forks?' + urlencode({'id': resource['id']})
    return (
        f'<section class="post-actions" id="post-actions" {data} aria-label="Post actions">'
        + '<div class="post-action-row">'
        + buttons
        + '<a class="bookmarks-link" href="/bookmarks" data-copy="collection">My saved posts</a>'
        + f'<a class="bookmarks-link" href="{escape(proof_url, quote=True)}" data-copy="proofs">View proof records</a>'
        + f'<a class="bookmarks-link" href="{escape(forks_url, quote=True)}" data-copy="forks">View branches</a></div>'
        + '<p role="status" aria-live="polite"></p>'
        + '<form hidden><label for="post-comment">Your comment</label>'
        + '<textarea id="post-comment" maxlength="20000" required></textarea>'
        + '<button type="submit">Send comment</button></form></section>'
    )
