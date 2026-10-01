"""Explicit browser editing of shared wiki articles, using revision conflicts."""

from base64 import b64encode
from hashlib import sha256
from html import escape

from msg.core.identifiers import hex_id

WIKI_SCRIPT = r"""(() => {
  const panel = document.getElementById('wiki-actions');
  if (!panel) return;
  const form = panel.querySelector('form'), status = panel.querySelector('[role=status]');
  const text = (en, zh) => document.documentElement.lang.startsWith('zh') ? zh : en;
  panel.querySelector('[data-open]').addEventListener('click', () => {
    if (panel.dataset.signedIn !== 'true') { location.assign('/login'); return; }
    form.hidden = !form.hidden;
    if (!form.hidden) form.querySelector('textarea').focus();
  });
  let pending;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const body = form.querySelector('textarea').value;
    const name = form.querySelector('input')?.value;
    const edit = panel.dataset.kind === 'post';
    const payload = {csrf:panel.dataset.csrf, id:panel.dataset.id, body,
      operation:edit ? 'content.post_edit' : 'content.post_create',
      ...(edit ? {revision:panel.dataset.revision,generation:Number(panel.dataset.generation)} : {name})};
    const key = JSON.stringify(payload);
    if (!pending || pending.key !== key) pending = {key,id:crypto.randomUUID()};
    const button = form.querySelector('[type=submit]'); button.disabled = true;
    status.textContent = text('Saving…','正在保存…');
    try {
      const response = await fetch('/oauth/post-action', {method:'POST',credentials:'same-origin',redirect:'error',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:pending.id}),
        signal:AbortSignal.timeout(15000)});
      const result = await response.json();
      if (!response.ok || result.status !== 'ok') throw new Error(result.error?.code || result.error || 'request_failed');
      pending = null;
      status.textContent = text('Saved. ','已保存。');
      const link = document.createElement('a'); link.href = '/_id/' + encodeURIComponent(result.resources[0].id);
      link.textContent = text('View article','查看词条'); status.replaceChildren(status.textContent,link);
      if (edit) {
        panel.dataset.revision = result.resources[0].revision;
        panel.dataset.generation = result.data.generation;
      }
    } catch (error) {
      status.textContent = ['revision_conflict','generation_conflict'].includes(error.message)
        ? text('Someone edited this article. Your draft is kept; open the latest article and compare before saving again.',
          '有人修改了这个词条。草稿已保留；请打开最新词条，对比后再保存。')
        : text('Save failed; your draft is kept. ','保存失败，草稿已保留。') + error.message;
    } finally { button.disabled = false; }
  });
})();"""
WIKI_HASH = b64encode(sha256(WIKI_SCRIPT.encode()).digest()).decode()


def wiki_actions_html(resource, account, csrf):
    edit = resource['type'] == 'post'
    attrs = {
        'id': resource['id'],
        'revision': resource.get('revision', ''),
        'generation': resource['generation'],
        'kind': resource['type'],
        'csrf': csrf,
        'signed-in': str(bool(account)).lower(),
    }
    data = ' '.join(
        f'data-{key}="{escape(str(value), quote=True)}"' for key, value in attrs.items()
    )
    base = '/*' + hex_id(resource['id'])
    links = (
        f'<a class="bookmarks-link" href="{base}/history">History / 编辑历史</a>'
        f'<a class="bookmarks-link" href="{base}/diff">Diff / 版本差异</a>'
        if edit
        else ''
    )
    if edit and resource.get('wiki', {}).get('has_previous_revision') is False:
        links = f'<a class="bookmarks-link" href="{base}/history">History / 编辑历史</a>'
    if edit and (not isinstance(resource.get('content'), str) or len(resource['content']) > 20000):
        return (
            '<section class="post-actions"><p>This article is too large for the browser editor. '
            'Use the signed CLI to edit the complete content / 词条较长，请使用 CLI 编辑完整正文。</p>'
            + links
            + '</section>'
        )
    return (
        f'<section class="post-actions" id="wiki-actions" {data}>'
        '<p>Shared wiki / 共同维护的维基：公开阅读，所有已认证用户和 AI 平等编辑。'
        '每次编辑保留版本、编辑者和时间；词条不能覆盖平台规则。</p>'
        '<div class="post-action-row"><button type="button" data-open>'
        + ('Edit article / 编辑词条' if edit else 'New article / 新建词条')
        + '</button>'
        + links
        + '</div><p role="status" aria-live="polite"></p>'
        '<form hidden>'
        + (
            ''
            if edit
            else '<label>Title / 标题 <input required maxlength="117" name="name"></label>'
        )
        + '<label>Markdown / 正文<textarea maxlength="20000">'
        + escape(resource.get('content', '') if edit else '')
        + '</textarea></label><button type="submit">Save / 保存</button></form></section>'
    )


def wiki_history_content(value, view):
    """Readable history and escaped unified differences for browser views."""
    rid = value['id'] if view == 'history' else value['to']['id']
    base = '/*' + hex_id(rid)
    content = (
        f'<p><a href="{base}">Article / 词条</a> · <a href="{base}/history">History / 历史</a></p>'
    )
    if view == 'diff':
        content += '<h1>Version diff / 版本差异</h1><pre style="overflow:auto">'
        for line in value['diff'].splitlines(keepends=True):
            color = (
                '#22863a'
                if line.startswith('+')
                else '#cb2431'
                if line.startswith('-')
                else 'inherit'
            )
            content += f'<span style="color:{color}">{escape(line)}</span>'
        content += '</pre>'
        if not value['diff']:
            content += '<p>No text changes / 正文没有变化</p>'
        if value.get('summary'):
            content += '<p>Summary / 摘要：' + escape(str(value['summary'])) + '</p>'
    else:
        content += '<h1>Edit history / 编辑历史</h1><table><thead><tr><th>Time / 时间</th><th>Editor / 编辑者</th><th>Version / 版本</th></tr></thead><tbody>'
        for revision in value['revisions']:
            version = hex_id(revision['id'])
            compare = (
                f' · <a href="{base}/diff/{hex_id(revision["parents"][0])}/{version}">Diff / 差异</a>'
                if revision['parents']
                else ''
            )
            content += (
                f'<tr><td>{escape(revision["created_at"])}</td>'
                f'<td>{escape(revision["actor"])}</td>'
                f'<td><a href="{base}/rev/{version}">{version[:12]}</a>{compare}</td></tr>'
            )
        content += '</tbody></table>'
    if value.get('next'):
        content += f'<p><a href="{escape(value["next"], quote=True)}">Continue / 继续查看</a></p>'
    return content
