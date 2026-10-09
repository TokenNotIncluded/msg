"""统一登录与登录方式管理；表单不携带 MSG wire packet。"""

from html import escape

from msg.transports.oauth_http import fields, page

PROVIDER_NAMES = {
    'agentid': 'AgentID',
    'google': 'Google',
    'github': 'GitHub',
    'chatgpt': 'ChatGPT',
    'email': '邮箱',
    'passkey': 'Passkey',
}

CUSTODY_CONTROL = (
    '<p><label style="display:flex;align-items:flex-start;gap:10px;min-height:44px">'
    '<input type="checkbox" name="custody" value="yes" required '
    'style="flex:0 0 20px;width:20px;height:20px;min-height:20px;padding:0;margin:4px 0">'
    '<span>我理解服务器会保管签名私钥和加密私钥。 / '
    'The server holds my private keys.</span></label></p>'
)

FINISH_SCRIPT = """(() => {
  const form = document.getElementById('finish-login');
  if (form) form.requestSubmit();
})();"""

APPROVAL_SCRIPT = """(() => {
  const form = document.getElementById('approve-login');
  if (form) setTimeout(() => form.requestSubmit(), 3000);
})();"""


def provider_form(provider, token, *, mode='login', handle=False):
    label = PROVIDER_NAMES[provider]
    body = '<form method="post" action="/-/login/start">' + fields({
        'csrf': token,
        'provider': provider,
        'mode': mode,
    })
    if handle:
        body += (
            '<p><label for="new-handle">用户名 / Username</label><br>'
            '<input id="new-handle" name="handle" required autocomplete="username" '
            'pattern="[a-z][a-z0-9\\-]{1,40}" minlength="2" maxlength="41"></p>'
        ) + CUSTODY_CONTROL
    return body + '<button type="submit">Continue with ' + escape(label) + '</button></form>'


def email_form(token, *, mode='login', handle=False):
    body = (
        '<form method="post" action="/-/login/email/start">'
        + fields({'csrf': token, 'mode': mode})
        + '<p><label for="email">邮箱 / Email</label><br>'
        '<input id="email" name="email" type="email" autocomplete="email" required maxlength="254"></p>'
    )
    if handle:
        body += (
            '<p><label for="email-handle">用户名 / Username</label><br>'
            '<input id="email-handle" name="handle" required autocomplete="username" '
            'pattern="[a-z][a-z0-9\\-]{1,40}" minlength="2" maxlength="41"></p>'
        ) + CUSTODY_CONTROL
    return body + '<button type="submit">发送登录验证码 / Send sign-in code</button></form>'


def login_page(
    token, providers, *, email=False, passkey=False, register=False, passkey_markup='', script=''
):
    title = '注册 MSG / Join MSG' if register else '登录 MSG / Sign in to MSG'
    body = '<p>人类与 Agent 使用同一个账号、同一份身份和权限。 / One MSG identity for you and your agents.</p>'
    body += '<p>使用已绑定方式完成验证后，5 分钟内可确认添加或移除登录方式；普通只读会话需要重新确认身份。</p>'
    if register:
        body += '<p>仅 Google 或邮箱可以注册。新账号使用平台托管密钥；以后可以迁移为自持。</p>'
        if 'google' in providers:
            body += provider_form('google', token, mode='register', handle=True)
    else:
        for provider in providers:
            body += provider_form(provider, token)
        if any(provider in providers for provider in ('github', 'chatgpt', 'agentid')):
            body += (
                '<p>GitHub／ChatGPT／AgentID 须先绑定已有 MSG 账号；相同邮箱不会自动合并账号。</p>'
            )
    if email:
        body += email_form(token, mode='register' if register else 'login', handle=register)
    if passkey and not register:
        body += passkey_markup
    if not providers and not email and not passkey:
        body += '<p>外部登录方式尚未配置。 / External sign-in is not configured yet.</p>'
    body += (
        '<p><a href="/oauth/login">用已有 MSG 私钥批准登录 / Approve with your MSG identity</a></p>'
        + (
            '<p><a href="/login">已有账号？登录 / Already registered? Sign in</a></p>'
            if register
            else '<p><a href="/register">用 Google 或邮箱注册 / Register with Google or email</a></p>'
        )
    )
    if not register:
        body += '<p><a href="/account/login-methods">管理已绑定的登录方式 / Manage sign-in methods</a></p>'
    return page(title, body, script=script)


def finish_page(token, state):
    return page(
        '完成登录 / Finish sign-in',
        '<p>正在验证登录。 / Verifying sign-in.</p>'
        '<form id="finish-login" method="post" action="/-/login/complete">'
        + fields({'csrf': token, 'state': state})
        + '<button type="submit">继续 / Continue</button></form>',
        script=FINISH_SCRIPT,
    )


def email_code_page(token, challenge):
    return page(
        '检查邮箱 / Check your email',
        '<p>输入收到的 12 位验证码，10 分钟内有效，只能使用一次。 / '
        'Enter the 12-digit code. It expires in 10 minutes and can be used once.</p>'
        '<form method="post" action="/-/login/email/complete">'
        + fields({'csrf': token, 'challenge': challenge})
        + '<p><label for="code">验证码 / Code</label><br>'
        '<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" '
        'pattern="[0-9]{12}" minlength="12" maxlength="12" required></p>'
        '<button type="submit">登录 / Sign in</button></form>',
    )


def approval_page(token, pending, code, *, removing=False, provider=''):
    return page(
        '确认移除登录方式 / Confirm removal' if removing else '确认绑定登录方式 / Confirm binding',
        '<p>本次操作：'
        + ('移除 ' if removing else '绑定 ')
        + escape(PROVIDER_NAMES.get(provider, provider))
        + ' 登录方式。</p>'
        '<p>请用已有 MSG 身份确认本次操作，5 分钟内有效。此确认不会改变私钥保管方式。</p>'
        '<p><code>msg auth approve ' + escape(code) + '</code></p>'
        '<p>如果已有 Google、邮箱或 Passkey 绑定，也可以重新登录后再发起管理操作。</p>'
        '<form id="approve-login" method="post" action="/-/login/approval">'
        + fields({'csrf': token, 'pending': pending})
        + '<button type="submit">已确认，继续 / Continue after approval</button></form>',
        script=APPROVAL_SCRIPT,
    )


def methods_page(
    token, providers, bindings, *, email=False, passkey_markup='', kind='registered', script=''
):
    custody = (
        '服务器保管私钥 / Server holds your keys'
        if kind == 'custodial'
        else '私钥由你自持 / You hold your keys'
    )
    body = '<p>' + custody + '。绑定登录方式不会改变密钥保管模式。</p><ul>'
    for binding in bindings:
        provider = binding['provider']
        body += (
            '<li>'
            + escape(PROVIDER_NAMES.get(provider, provider))
            + '<form method="post" action="/-/login/remove">'
            + fields({'csrf': token, 'binding_id': binding['binding_id']})
            + '<button type="submit">移除 / Remove</button></form></li>'
        )
    body += '</ul><h2>添加登录方式 / Add a sign-in method</h2>'
    for provider in providers:
        body += provider_form(provider, token, mode='bind')
    if email:
        body += email_form(token, mode='bind')
    body += passkey_markup + '<p><a href="/">返回首页 / Home</a></p>'
    return page('登录方式 / Sign-in methods', body, script=script)
