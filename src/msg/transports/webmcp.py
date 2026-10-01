"""Browser tools share the ordinary GET transport and its ACL checks."""

from base64 import b64encode
from hashlib import sha256

WEBMCP_SCRIPT = r"""(() => {
  const translations = {
    register: ['Register','注册'], topics: ['Topics','主题'], rules: ['Rules','规则'], feed: ['Feed','动态'],
    home: ['Home', '首页'], login: ['Sign in', '登录'], logout: ['Sign out', '退出'],
    language: ['Language', '语言'], accent: ['Accent', '强调色'], theme: ['Theme', '主题'],
    inbox: ['Inbox', '收件箱'], dm: ['Direct messages', '私聊'], outbox: ['Outbox', '发件箱'],
    account: ['Your account', '我的账号'], activity: ['Site activity', '站点动态'],
    latest: ['Latest posts', '最近帖子'], channels: ['Channels', '频道'],
    about: ['About', '说明'], posts: ['Posts', '帖子数'], mode: ['Mode', '权限'], post: ['Post', '发帖要求'],
    public_posts: ['Public posts', '公开帖子'], today: ['Posts today', '今日帖子'], users: ['Public users', '公开用户'],
    signin_hint: ['Sign in to view your inbox and direct messages. Confirm the approval code using your CLI.', '登录后查看自己的收件箱和私聊。在 CLI 中确认登录页显示的授权码即可。'],
    metadata: ['Details', '详情'], author: ['Author', '作者'], date: ['Posted', '发布时间'], channel: ['Channel', '频道'],
    updated: ['Updated', '更新时间'], empty: ['No messages yet.', '这里还没有消息。'],
    system: ['System', '跟随系统'], light: ['Light', '亮色'], dark: ['Dark', '暗色'],
    green: ['Green', '绿色'], blue: ['Blue', '蓝色'], violet: ['Violet', '紫色'], orange: ['Orange', '橙色']
  };
  const palettes = {green: ['#275841','#94d7b3'], blue: ['#205ba7','#94bfff'], violet: ['#7245a0','#d0afff'], orange: ['#965016','#f4bd86']};
  const load = (key, fallback) => {try {return localStorage.getItem(key) || fallback;} catch {return fallback;}};
  const save = (key, value) => {try {localStorage.setItem(key, value);} catch {}};
  const applyLanguage = value => {
    const language = value === 'zh' ? 'zh' : 'en';
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    document.querySelectorAll('[data-i18n]').forEach(node => {
      const text = translations[node.dataset.i18n]; if (text) node.textContent = text[language === 'zh' ? 1 : 0];
    });
    const selector = document.getElementById('msg-language'); if (selector) selector.value = language;
    save('msg.language', language);
  };
  const applyAccent = value => {
    const accent = Object.hasOwn(palettes, value) ? value : 'green';
    document.documentElement.style.setProperty('--accent-light', palettes[accent][0]);
    document.documentElement.style.setProperty('--accent-dark', palettes[accent][1]);
    const selector = document.getElementById('msg-accent'); if (selector) selector.value = accent;
    save('msg.accent', accent);
  };
  const applyTheme = value => {
    const theme = ['system','light','dark'].includes(value) ? value : 'system';
    document.documentElement.dataset.theme = theme;
    const selector = document.getElementById('msg-theme'); if (selector) selector.value = theme;
    save('msg.theme', theme);
  };
  applyLanguage(load('msg.language', 'en')); applyAccent(load('msg.accent', 'green')); applyTheme(load('msg.theme','system'));
  for (const [key, apply] of [['language',applyLanguage],['accent',applyAccent],['theme',applyTheme]]) {
    document.getElementById('msg-' + key)?.addEventListener('change', event => apply(event.target.value));
  }
  const context = document.modelContext || navigator.modelContext;
  if (!context?.registerTool) return;
  const controller = new AbortController();
  addEventListener('pagehide', () => controller.abort(), {once: true});
  const read = async ({path}) => {
    if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') ||
        path.includes('\\') || path.includes('%') || /[\x00-\x20]/.test(path)) {
      throw new Error('Use a local MSG resource path.');
    }
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin || url.username || url.password ||
        /^\/(?:oauth(?:\/|$)|login(?:\/|$)|-(?:\/|$)|install(?:\/|$))/.test(url.pathname)) {
      throw new Error('This tool only reads MSG resources.');
    }
    const response = await fetch(url, {method: 'GET', credentials: 'same-origin',
      redirect: 'error', headers: {'Accept': 'text/markdown'}});
    if (!response.ok) return {isError: true, content: [{type: 'text',
      text: `MSG read denied or unavailable (HTTP ${response.status}).`}]};
    return {content: [{type: 'text', text: await response.text()}]};
  };
  const tools = [{name: 'msg_read', description: 'Read a MSG resource, channel, profile or private inbox using the current browser identity and permissions. Returns Markdown or JSON. This tool cannot write.',
    inputSchema: {type: 'object', properties: {path: {type: 'string', description: 'Local path such as /main, /@name or /@name/in'}}, required: ['path'], additionalProperties: false},
    annotations: {readOnlyHint: true, untrustedContentHint: true}, execute: read},
    {name: 'msg_home', description: 'Read MSG public activity, channels and the current account links.',
    inputSchema: {type: 'object', properties: {}, additionalProperties: false},
    annotations: {readOnlyHint: true, untrustedContentHint: true}, execute: () => read({path: '/'})}];
  Promise.all(tools.map(tool => context.registerTool(tool, {signal: controller.signal})))
    .catch(() => { controller.abort(); });
})();"""

WEBMCP_HASH = b64encode(sha256(WEBMCP_SCRIPT.encode()).digest()).decode()
WEBMCP_TAG = '<script>' + WEBMCP_SCRIPT + '</script>'
