"""Hash-authorized sign-in polling, independent of optional browser tools."""

LOGIN_POLL_SCRIPT = r"""(() => {
  const status = document.getElementById('status');
  if (!status) return;
  let timer, requestController, failures = 0, paused = false, complete = false, epoch = 0;
  const text = (en, zh) => document.documentElement.lang.startsWith('zh') ? zh : en;
  const show = (en, zh) => {
    status.removeAttribute('data-i18n');
    status.textContent = text(en, zh);
  };
  const schedule = delay => {
    clearTimeout(timer);
    if (!paused && !complete) timer = setTimeout(poll, delay);
  };
  const poll = async () => {
    if (paused || complete) return;
    const generation = epoch;
    const controller = new AbortController();
    requestController = controller;
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/oauth/login/poll', {
        method: 'POST', credentials: 'same-origin', redirect: 'error',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({csrf: status.dataset.csrf}), signal: controller.signal
      });
      const data = await response.json();
      if (paused || generation !== epoch) return;
      if (response.ok && data.logged_in === true) {
        complete = true;
        show('Signed in.', '已登录。');
        const link = document.createElement('a');
        link.href = '/'; link.textContent = text('Back to home', '返回首页');
        status.append(' ', link);
      } else if (['authorization_pending', 'slow_down'].includes(data.error)) {
        failures = 0;
        show('Waiting for approval…', '等待确认…');
        schedule(data.error === 'slow_down' ? 15000 : 5000);
      } else if (response.status >= 500) {
        throw new Error('Temporarily unavailable');
      } else {
        complete = true;
        show('Sign-in was not completed. Reopen the sign-in page.', '登录未完成，请重新打开登录页。');
        const link = document.createElement('a');
        link.href = '/login'; link.textContent = text('Sign in again', '重新登录');
        status.append(' ', link);
      }
    } catch {
      if (!paused && generation === epoch) {
        failures += 1;
        show('Connection interrupted. Retrying…', '连接中断，正在重试…');
        schedule(Math.min(30000, 5000 * 2 ** Math.min(failures - 1, 3)));
      }
    } finally {
      clearTimeout(timeout);
      if (requestController === controller) requestController = null;
    }
  };
  addEventListener('pagehide', () => {
    paused = true; epoch += 1; clearTimeout(timer); requestController?.abort();
  });
  addEventListener('pageshow', event => {
    if (event.persisted) { paused = false; clearTimeout(timer); schedule(5000); }
  });
  schedule(5000);
})();"""
