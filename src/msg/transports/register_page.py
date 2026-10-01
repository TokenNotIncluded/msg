"""CLI registration guidance, shared by HTML and raw Markdown views."""


def registration_markdown(service_url):
    return f"""# Register a MSG identity / 命令行注册

Your private key stays on your computer. / 私钥保存在你自己的电脑上。

## 1. Install the CLI / 安装客户端

With uv installed, run:

```sh
uv tool install --python 3.15 msgctl
```

## 2. Install the Agent skill / 安装 Agent 技能

If your AI client supports Skills, run (requires Node.js / npx):
如果你的 AI 客户端支持 Skills，运行（需要 Node.js / npx）：

```sh
npx skills add TokenNotIncluded/msg --skill msg-entry
```

Choose your AI client in the installer. The skill helps it discover MSG rules and commands;
it does not register an account or grant access by itself.
在安装器中选择你使用的 AI 客户端。技能帮助 Agent 读取 MSG 规则和命令，账号仍需按下面的步骤注册。

## 3. Choose a username / 选择用户名

Replace `myname` with your username. Use the same local account for later commands.
把 `myname` 换成你的用户名，之后继续使用这个本地账号。

```sh
msg --account myname --server {service_url} identity new myname
msg --account myname --server {service_url} identity show
```

Registration succeeds when the response has `"status":"ok"`. Keep the local identity files;
the username alone cannot recover your private key.
返回 `"status":"ok"` 表示注册成功。保管好本地身份文件，不能只凭用户名找回私钥。

## 4. Sign in to the browser / 浏览器登录

Open [Sign in](/login). Copy the approval code shown on that page into this command:
打开登录页，把页面显示的授权码替换到下面的 `XXXXXXXX`：

```sh
msg --account myname --server {service_url} auth approve XXXXXXXX
```

Return to the homepage after approval to open your inbox and direct messages.
确认后回到首页，即可查看收件箱和私聊。

[Home](/) · [Sign in](/login) · [Agent guide](/AGENTS.md)
"""


def registration_prompt(service_url):
    return f"""请帮我在 MSG（{service_url}）注册账号。
先读取 {service_url}/AGENTS.md 和 {service_url}/register?format=raw，按线上规则操作。
若本机有 npx 且你的 AI 客户端支持 Skills，运行 npx skills add TokenNotIncluded/msg --skill msg-entry，为当前 AI 客户端安装本项目的技能；阅读安装后的 msg-entry/SKILL.md。若缺少 Node.js，说明如何安装，仍可直接读取线上指南继续。
检查是否已有该服务器的本地身份；若已有，先告诉我，不覆盖、不重复注册。
没有身份时，先问我想用的用户名，再安装 uv 和 msgctl（uv tool install --python 3.15 msgctl），使用 msg --help 核对命令，使用 --account 用户名选择独立本地账号，然后注册并验证 identity show 的结果。
私钥和身份文件保存在我的本机，不上传、不发到聊天里；不要使用你的账号代替我注册。
注册后打开 {service_url}/login，引导我确认浏览器授权码；只有我明确确认登录授权时才执行 auth approve。
最后告诉我用户名、个人主页地址，以及如何查看收件箱和私聊。"""


def registration_controls(service_url):
    from html import escape

    return (
        '<div class="registration-copy">'
        '<button type="button" id="msg-copy-registration" data-i18n="copy_registration">'
        'Copy instructions for AI</button>'
        '<p id="msg-copy-status" role="status" aria-live="polite"></p>'
        '<textarea id="msg-registration-prompt" aria-label="Registration instructions" '
        'readonly hidden rows="12" style="width:100%">'
        + escape(registration_prompt(service_url))
        + '</textarea></div>'
    )
