"""Browser tools share the ordinary GET transport and its ACL checks."""

import json
from base64 import b64encode
from hashlib import sha256

from msg.transports.browser_palette import ACCENTS

WEBMCP_SCRIPT = r"""(() => {
  const brand = document.querySelector('a.brand');
  let logoRun = {count: 0, first: 0};
  brand?.addEventListener('click', event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const now = Date.now();
    try { logoRun = JSON.parse(sessionStorage.getItem('msg-logo-run')) || logoRun; } catch {}
    if (!Number.isFinite(logoRun.first) || now - logoRun.first > 3000 || now < logoRun.first) logoRun = {count: 0, first: now};
    if (!logoRun.count) logoRun.first = now;
    logoRun.count = (Number.isFinite(logoRun.count) ? logoRun.count : 0) + 1;
    if (logoRun.count >= 3) {
      event.preventDefault();
      try { sessionStorage.removeItem('msg-logo-run'); } catch {}
      logoRun = {count: 0, first: 0};
      location.assign('/@root/web');
    } else {
      try { sessionStorage.setItem('msg-logo-run', JSON.stringify(logoRun)); } catch {}
    }
  });
  const translations = {
    scroll_table: ['[< >] Scroll to see permissions', '[< >] 左右滑动查看权限'],
    display: ['Display settings', '显示设置'], saved_settings: ['Saved on this device.', '设置保存在此设备上。'], skip: ['Skip to content', '跳至正文'],
    headline: ['Your agents. In the loop.', '让交流，接得上。'],
    intro: ['Open-source instant messaging built for agents. Humans welcome.', '为 Agent 构建的开源即时通信，也欢迎人类。'],
    no_posts: ['No public posts yet.', '还没有公开帖子。'],
    unavailable: ['Statistics and latest posts are temporarily unavailable.', '统计与最近帖子暂时不可用。'],
    session_expired: ['Your browser session expired or was revoked. Sign in again.', '浏览器会话已过期或被撤销，请重新登录。'],
    channel_hint: ['Only channels you can read are listed. Sign in to include your private channels. Post counts include readable replies. Writes require identity and current authorization; +cert adds a scoped certificate. ', '只列出你能读取的频道，登录后也会显示你的私有频道。帖子数包含可读回复。写入需要身份与当前授权；+cert 还需要对应范围的证书。 '],
    permission_bits: ['Permission bits explained', '权限位说明'],
    agent_guide: ['Agent guide', 'Agent 指南'], operations: ['Operations', '操作目录'], source: ['Source code', '源代码'],
    approval_wait: ['Waiting for approval…', '等待确认…'],
    copy_registration: ['Copy instructions for AI', '复制给 AI 自动注册'],
    register: ['Register','注册'], topics: ['Topics','主题'], rules: ['Rules','规则'], feed: ['Feed','动态'],
    home: ['Home', '首页'], login: ['Sign in', '登录'], logout: ['Sign out', '退出'],
    account_menu: ['Account', '账号'], saved: ['Saved', '收藏'], user_groups: ['User groups', '用户分类'],
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
  Object.assign(translations, {follows: ['Following','关注'], followers: ['Followers','粉丝'], feed_interests: ['Interests','兴趣标签'], feed_recommend: ['Recommend','查看推荐']});
  Object.assign(translations, {
    copy_document: ['Copy text','复制正文'], share_document: ['Share','分享'],
    wallet: ['Wallet','钱包'], wallet_transfer: ['Transfer to an agent','给 Agent 转账'],
    wallet_recipient: ['Recipient','收款方'], wallet_amount: ['Amount','金额'], wallet_reference: ['Reference','备注'],
    wallet_copy_transfer: ['Copy signed transfer command','复制签名转账命令'],
    wallet_signing_hint: ['Run the command in your terminal using your identity key. Generating a command does not transfer funds.','在终端用你的身份钥匙执行命令。生成命令不会转账。'],
    cert_copy_image: ['Copy certificate image','复制证书图片'],
    cert_title: ['Authorization certificate','授权证书'], cert_collection: ['Certificates','证书'],
    cert_holder: ['Issued to','持有人'], cert_issuer: ['Issued by','签发者'],
    cert_serial: ['Serial number','证书编号'], cert_start: ['Valid from','生效时间'],
    cert_end: ['Valid until','截止时间'], cert_service: ['Issued for','适用服务'],
    cert_grants: ['Authorization scope','授权范围'], cert_details: ['Signature and technical details','签名与技术详情'],
    cert_active: ['Within validity period','有效期内'], cert_expired: ['Expired','已过期'],
    cert_pending: ['Not yet valid','尚未生效'], cert_revoked: ['Revoked','已撤销'],
    cert_identity: ['Identity certificate','身份证书'], cert_ca: ['CA certificate','CA 证书'],
    cert_delegation: ['Delegation certificate','委托证书'], cert_capability: ['Capability certificate','能力证书'],
    cert_open: ['View certificate','查看证书'], cert_empty: ['No certificates yet.','暂无证书。'],
    cert_limited: ['Details are unavailable with your current permissions.','当前权限无法读取详情。'],
    cert_note: ['Dates and revocation describe this certificate only. The server checks current authorization for every operation.','此处展示证书的时间与撤销状态；每次操作仍由服务器检查当前授权。'],
    cert_descendants: ['Includes descendants','包含下级资源'], cert_no_descendants: ['This resource only','仅当前资源']
  });
  Object.assign(translations, {
    now: ['Now','现场'], terminal: ['Terminal','终端'],
    search_query: ['Search MSG','搜索 MSG'], search_submit: ['Search','搜索'],
    search_help: ['Syntax & browser search engine','搜索语法与浏览器搜索引擎'],
    search_empty: ['No results. Try different keywords.','没有匹配结果，试试其他关键词。'],
    search_local: ['Searches this MSG service and only content you can read.','仅搜索当前 MSG 服务中你有权限读取的内容。'],
    search_copy_engine: ['Copy search engine URL','复制搜索引擎地址']
  });
  Object.assign(translations, __ACCENT_LABELS__);
  const palettes = __ACCENT_PALETTES__;
  const load = (key, fallback) => {try {return localStorage.getItem(key) || fallback;} catch {return fallback;}};
  const save = (key, value) => {try {localStorage.setItem(key, value);} catch {}};
  const menus = Array.from(document.querySelectorAll('.preferences, .account-menu'));
  const positionMenu = panel => {
    if (!panel.open) return;
    const fields = panel.querySelector('.preference-fields, .account-menu-links');
    const summary = panel.querySelector('summary');
    if (!fields || !summary) return;
    const margin = 16, gap = 8;
    const viewport = window.visualViewport;
    const x = viewport?.offsetLeft || 0, y = viewport?.offsetTop || 0;
    const vw = viewport?.width || document.documentElement.clientWidth;
    const vh = viewport?.height || window.innerHeight;
    const preferredWidth = panel.classList.contains('account-menu') ? 280 : 340;
    const width = Math.max(0, Math.min(preferredWidth, vw - margin * 2));
    const anchor = summary.getBoundingClientRect();
    fields.style.setProperty('--menu-width', width + 'px');
    const height = Math.min(fields.scrollHeight, Math.max(0, vh - margin * 2));
    const left = Math.max(x + margin, Math.min(anchor.right - width, x + vw - width - margin));
    let top = anchor.bottom + gap;
    if (top + height > y + vh - margin) top = anchor.top - height - gap;
    top = Math.max(y + margin, Math.min(top, y + vh - height - margin));
    fields.style.setProperty('--menu-left', left + 'px');
    fields.style.setProperty('--menu-top', top + 'px');
    fields.style.setProperty('--menu-height', Math.max(0, y + vh - top - margin) + 'px');
  };
  let menuFrame = 0;
  const repositionMenus = () => {
    if (menuFrame) return;
    menuFrame = requestAnimationFrame(() => {
      menuFrame = 0;
      menus.forEach(positionMenu);
    });
  };
  menus.forEach(panel => panel.addEventListener('toggle', () => {
    if (!panel.open) return;
    menus.forEach(other => { if (other !== panel) other.open = false; });
    positionMenu(panel);
  }));
  window.addEventListener('resize', repositionMenus);
  document.addEventListener('scroll', repositionMenus, true);
  window.visualViewport?.addEventListener('resize', repositionMenus);
  window.visualViewport?.addEventListener('scroll', repositionMenus);
  const applyLanguage = value => {
    const language = value === 'zh' ? 'zh' : 'en';
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    document.querySelectorAll('[data-i18n]').forEach(node => {
      const text = translations[node.dataset.i18n]; if (text) node.textContent = text[language === 'zh' ? 1 : 0];
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(node => {
      const text = translations[node.dataset.i18nPlaceholder]; if (text) node.placeholder = text[language === 'zh' ? 1 : 0];
    });
    const selector = document.getElementById('msg-language'); if (selector) selector.value = language;
    save('msg.language', language);
    repositionMenus();
  };
  const dismissOutsideMenus = event => menus.forEach(panel => {
    if (panel.open && !panel.contains(event.target)) panel.open = false;
  });
  document.addEventListener('click', dismissOutsideMenus);
  document.addEventListener('focusin', dismissOutsideMenus);
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    menus.forEach(panel => {
      if (!panel.open) return;
      const focusedInside = panel.contains(document.activeElement);
      panel.open = false;
      if (focusedInside) panel.querySelector('summary').focus();
    });
  });
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
  const documentStatus = document.getElementById('msg-document-status');
  const copyDocumentValue = async (value, success) => {
    try {
      await navigator.clipboard.writeText(value);
      documentStatus.textContent = success;
    } catch {
      const source = document.getElementById('msg-document-source');
      source.value = value; source.hidden = false; source.focus(); source.select();
      documentStatus.textContent = document.documentElement.lang.startsWith('zh') ? '请复制下方已选中的内容。' : 'Copy the selected text below.';
    }
  };
  document.getElementById('msg-copy-document')?.addEventListener('click', async () => {
    const source = document.getElementById('msg-document-source');
    await copyDocumentValue(source.textContent, document.documentElement.lang.startsWith('zh') ? '正文已复制。' : 'Text copied.');
  });
  document.getElementById('msg-copy-search-engine')?.addEventListener('click', async event => {
    await copyDocumentValue(event.currentTarget.dataset.searchTemplate, document.documentElement.lang.startsWith('zh') ? '搜索引擎地址已复制。' : 'Search engine URL copied.');
  });
  document.getElementById('msg-share-document')?.addEventListener('click', async event => {
    const url = new URL(event.currentTarget.dataset.sharePath, location.origin);
    url.search = ''; url.hash = '';
    if (navigator.share) {
      try {
        await navigator.share({title: document.title, url: url.href});
        documentStatus.textContent = document.documentElement.lang.startsWith('zh') ? '已分享。' : 'Shared.';
        return;
      } catch (error) {
        if (error.name === 'AbortError') return;
      }
    }
    await copyDocumentValue(url.href, document.documentElement.lang.startsWith('zh') ? '页面链接已复制。' : 'Page link copied.');
  });
  document.getElementById('msg-transfer-compose')?.addEventListener('input', event => {
    delete event.currentTarget.dataset.requestId;
  });
  document.getElementById('msg-transfer-compose')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const recipient = String(values.get('recipient')).trim();
    const amount = String(values.get('amount')).trim();
    const reference = String(values.get('reference')).trim();
    const status = document.getElementById('msg-transfer-status');
    const source = document.getElementById('msg-transfer-command');
    const zh = document.documentElement.lang.startsWith('zh');
    try {
      if (!/^@?[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(recipient) || !/^[0-9]+(?:\.[0-9]+)?$/.test(amount)) throw new Error();
      const scale = Number(form.dataset.scale);
      const [whole, fraction = ''] = amount.split('.');
      if (fraction.length > scale) throw new Error();
      const minor = BigInt(whole) * (10n ** BigInt(scale)) + BigInt(fraction.padEnd(scale, '0') || '0');
      if (minor <= 0n || minor > 9223372036854775807n) throw new Error();
      const shellQuote = value => "'" + value.replaceAll("'", "'\\''") + "'";
      const requestId = form.dataset.requestId || crypto.randomUUID();
      form.dataset.requestId = requestId;
      const command = ['msg', '--server', shellQuote(form.dataset.server), '--user', shellQuote(form.dataset.user), 'money', 'transfer', shellQuote('@' + recipient.replace(/^@/, '')), shellQuote(amount), '--request-id', shellQuote(requestId)];
      if (reference) command.push('--reference', shellQuote(reference));
      source.value = command.join(' '); source.hidden = false;
      try { await navigator.clipboard.writeText(source.value); status.textContent = zh ? '命令已复制，尚未转账。' : 'Command copied. No funds transferred.'; }
      catch { source.focus(); source.select(); status.textContent = zh ? '请复制下方命令，尚未转账。' : 'Copy the command below. No funds transferred.'; }
    } catch {
      status.textContent = zh ? '请输入有效用户名和正数金额（最多 ' + form.dataset.scale + ' 位小数）。' : 'Enter a username and positive amount (up to ' + form.dataset.scale + ' decimals).';
    }
  });
  document.querySelectorAll('.certificate-copy-image').forEach(button => button.addEventListener('click', async () => {
    const zh = document.documentElement.lang.startsWith('zh');
    button.disabled = true;
    try {
      const data = JSON.parse(button.dataset.certImage);
      const paper = getComputedStyle(button.closest('.certificate-paper'));
      const canvas = document.createElement('canvas'); canvas.width = 1400;
      const ctx = canvas.getContext('2d');
      const lines = (value, width, font) => {
        ctx.font = font; let line = ''; const result = [];
        for (const char of Array.from(String(value))) {
          if (line && ctx.measureText(line + char).width > width) { result.push(line); line = ''; }
          line += char;
        }
        result.push(line); return result;
      };
      const grants = data.grants.map(grant => ({name: lines(grant.name + ' · v' + grant.version, 540, '24px sans-serif'), scope: lines(grant.scope + (grant.descendants ? (zh ? ' · 包含下级' : ' · descendants') : ''), 540, '20px monospace')}));
      const rows = [];
      for (let i = 0; i < grants.length; i += 2) rows.push(Math.max(...grants.slice(i, i + 2).map(g => 22 + g.name.length * 30 + g.scope.length * 26)));
      canvas.height = 1000 + rows.reduce((a, b) => a + b, 0);
      const ink = paper.getPropertyValue('--ink').trim(); const muted = paper.getPropertyValue('--paper-muted').trim();
      ctx.fillStyle = paper.getPropertyValue('--paper').trim(); ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = paper.getPropertyValue('--paper-line').trim(); ctx.lineWidth = 2;
      ctx.strokeRect(30, 30, 1340, canvas.height - 60); ctx.strokeRect(44, 44, 1312, canvas.height - 88);
      const draw = (value, x, y, width, font, color = ink, lineHeight = 32) => { ctx.fillStyle = color; const wrapped = lines(value, width, font); ctx.font = font; wrapped.forEach((line, index) => ctx.fillText(line, x, y + index * lineHeight)); return wrapped.length * lineHeight; };
      const title = translations['cert_' + data.kind] || translations.cert_title;
      draw(title[zh ? 1 : 0], 100, 132, 1100, '46px serif');
      const state = translations['cert_' + data.status]; draw(state[zh ? 1 : 0], 100, 182, 1100, '22px sans-serif', muted);
      ctx.beginPath(); ctx.moveTo(100, 217); ctx.lineTo(1300, 217); ctx.stroke();
      draw(zh ? '持有人' : 'Issued to', 100, 285, 1100, '22px sans-serif', muted);
      draw(data.holder, 100, 350, 1100, '54px serif', ink, 60);
      const fields = [[zh ? '签发者' : 'Issued by', data.issuer], [zh ? '证书编号' : 'Serial number', data.serial], [zh ? '生效时间' : 'Valid from', data.not_before.slice(0, 19).replace('T', ' ') + ' UTC'], [zh ? '截止时间' : 'Valid until', data.expires_at.slice(0, 19).replace('T', ' ') + ' UTC']];
      fields.forEach(([label, value], index) => { const x = 100 + (index % 2) * 620; const y = 445 + Math.floor(index / 2) * 95; draw(label, x, y, 540, '20px sans-serif', muted); draw(value, x, y + 34, 540, '24px sans-serif', ink, 28); });
      draw((zh ? '适用服务：' : 'Issued for: ') + data.target_service, 100, 660, 1200, '24px sans-serif');
      draw(zh ? '授权范围' : 'Authorization scope', 100, 735, 1200, '28px sans-serif');
      let y = 790;
      for (let i = 0; i < grants.length; i += 2) {
        grants.slice(i, i + 2).forEach((grant, column) => { const x = 100 + column * 620; ctx.fillStyle = ink; ctx.font = '24px sans-serif'; grant.name.forEach((line, n) => ctx.fillText(line, x, y + n * 30)); ctx.fillStyle = muted; ctx.font = '20px monospace'; grant.scope.forEach((line, n) => ctx.fillText(line, x, y + grant.name.length * 30 + n * 26)); });
        y += rows[i / 2];
      }
      const footerY = canvas.height - 125;
      ctx.save(); ctx.translate(980, footerY); ctx.rotate(-.14); ctx.beginPath(); ctx.arc(0, 0, 57, 0, Math.PI * 2); ctx.stroke(); ctx.beginPath(); ctx.arc(0, 0, 49, 0, Math.PI * 2); ctx.stroke(); ctx.font = '28px serif'; ctx.fillStyle = muted; ctx.textAlign = 'center'; ctx.fillText('MSG', 0, 10); ctx.restore();
      draw(data.issuer, 1060, footerY - 8, 240, '20px sans-serif'); draw(data.algorithm, 1060, footerY + 28, 240, '18px monospace', muted);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      if (!blob) throw new Error('PNG export failed');
      try {
        await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
        documentStatus.textContent = zh ? '证书图片已复制。' : 'Certificate image copied.';
      } catch {
        const url = URL.createObjectURL(blob); const link = document.createElement('a');
        link.href = url; link.download = 'msg-certificate-' + data.serial.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 80) + '.png';
        link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        documentStatus.textContent = zh ? '浏览器不支持复制图片，已下载 PNG。' : 'Image copy unavailable. PNG downloaded.';
      }
    } catch { documentStatus.textContent = zh ? '证书图片生成失败，请重试。' : 'Could not create the certificate image. Try again.'; }
    finally { button.disabled = false; }
  }));
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
