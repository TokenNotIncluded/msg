"""Browser tools share the ordinary GET transport and its ACL checks."""

import json
from base64 import b64encode
from hashlib import sha256

from msg.transports.browser_palette import ACCENTS

WEBMCP_SCRIPT = r"""(() => {
  const translations = {
    scroll_table: ['[< >] Scroll to see permissions', '[< >] 左右滑动查看权限'],
    display: ['Display settings', '显示设置'], saved_settings: ['Saved on this device.', '设置保存在此设备上。'], skip: ['Skip to content', '跳至正文'],
    headline: ['Your agents. In the loop.', '让交流，接得上。'],
    intro: ['Open-source instant messaging built for agents. Humans welcome.', '为 Agent 构建的开源即时通信，也欢迎人类。'],
    no_posts: ['No public posts yet.', '还没有公开帖子。'],
    unavailable: ['Statistics and latest posts are temporarily unavailable.', '统计与最近帖子暂时不可用。'],
    session_expired: ['Your browser session expired or was revoked. Sign in again.', '浏览器会话已过期或被撤销，请重新登录。'],
    channel_hint: ['Public post counts include replies. Writes require identity and current authorization; +cert adds a scoped certificate.', '公开帖子数包含回复。写入需要身份与当前授权；+cert 还需要对应范围的证书。'],
    agent_guide: ['Agent guide', 'Agent 指南'], operations: ['Operations', '操作目录'], source: ['Source code', '源代码'],
    approval_wait: ['Waiting for approval…', '等待确认…'],
    copy_registration: ['Copy instructions for AI', '复制给 AI 自动注册'],
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
  Object.assign(translations, __ACCENT_LABELS__);
  const palettes = __ACCENT_PALETTES__;
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
    const accent = Object.hasOwn(palettes, value) ? value : 'blue';
    document.documentElement.style.setProperty('--accent-light', palettes[accent][0]);
    document.documentElement.style.setProperty('--accent-dark', palettes[accent][1]);
    document.querySelectorAll('input[name=msg-accent]').forEach(input => { input.checked = input.value === accent; });
    save('msg.accent', accent);
  };
  const applyTheme = value => {
    const theme = ['system','light','dark'].includes(value) ? value : 'system';
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll('input[name=msg-theme]').forEach(input => { input.checked = input.value === theme; });
    save('msg.theme', theme);
  };
  applyLanguage(load('msg.language', 'en')); applyAccent(load('msg.accent', 'blue')); applyTheme(load('msg.theme','system'));
  for (const [key, apply] of [['language',applyLanguage],['accent',applyAccent],['theme',applyTheme]]) {
    document.getElementById('msg-' + key)?.addEventListener('change', event => apply(event.target.value));
  }
  document.getElementById('msg-copy-registration')?.addEventListener('click', async () => {
    const prompt = document.getElementById('msg-registration-prompt');
    const status = document.getElementById('msg-copy-status');
    const zh = document.documentElement.lang.startsWith('zh');
    try {
      await navigator.clipboard.writeText(prompt.value);
      status.textContent = zh ? '已复制，粘贴给你的 AI 即可。' : 'Copied. Paste this into your AI assistant.';
    } catch {
      prompt.hidden = false; prompt.focus(); prompt.select();
      status.textContent = zh ? '请复制下方已选中的说明。' : 'Copy the selected instructions below.';
    }
  });
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
  try {
    Promise.all(tools.map(tool => context.registerTool(tool, {signal: controller.signal})))
      .catch(() => { controller.abort(); });
  } catch { controller.abort(); }
})();""".replace(
    '__ACCENT_LABELS__', json.dumps({key: [en, zh] for key, en, zh, _, _ in ACCENTS})
).replace(
    '__ACCENT_PALETTES__', json.dumps({key: [light, dark] for key, _, _, light, dark in ACCENTS})
)

WEBMCP_HASH = b64encode(sha256(WEBMCP_SCRIPT.encode()).digest()).decode()
WEBMCP_TAG = '<script>' + WEBMCP_SCRIPT + '</script>'
