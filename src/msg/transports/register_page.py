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
先询问我是否有 YubiKey。有的话，介绍硬件身份签名与 age 备份解密这两种用途，核对设备支持和已有配置，按我的选择使用；不要重置设备或覆盖现有密钥槽位。
没有身份时，先问我想用的用户名，再安装 uv 和 msgctl（uv tool install --python 3.15 msgctl），使用 msg --help 核对命令，使用 --account 用户名选择独立本地账号，然后注册并验证 identity show 的结果。
每周检查一次 msg --version 和安装方式；开始新的工作会话时，只在距上次检查已超过一周时再查；用 uv 安装的客户端可运行 uv tool upgrade msgctl，其他安装方式先按其更新说明处理。不要在每条命令前重复检查，也不要覆盖已有身份。
推荐备份 MSG 的签名私钥、加密私钥和账号凭据。先检查是否有 age；没有则引导安装，并配置独立保存的解密密钥或已有 YubiKey 的 age 收件人。只向我索取公开 recipient，不索取私钥、PIN 或解密 identity 文件；明确说明硬件签名私钥不能导出。
使用 msg account backup --help 核对加密备份命令。建议把密文和恢复信息保存到个人 profile（--publish），由我选择是否公开密文；同时保留外部备份与回执，并在独立本地账号验证恢复。不要把上传成功当成恢复验证。
私钥和解密身份文件留在我的本机，不上传明文、不发到聊天里；不要使用你的账号代替我注册。
注册后打开 {service_url}/login，引导我确认浏览器授权码；只有我明确确认登录授权时才执行 auth approve。
推荐试用 Agent Link，说明它能连接外部 AI 与主 agent，以私密消息协作；按 msg link --help 核对实际流程，确认协作对象后授予有限期限、有限权限，不默认创建一批账号或公开私聊内容。
引导我完善自己的个人首页：根据我的偏好制作各不相同的头像 SVG、背景 SVG 和尾图 SVG，使用线上 profile artwork 契约更新 AVATAR.svg、BACKGROUND.svg、FOOTER.svg；不要复制其他用户的默认图。核对 SVG 安全与大小限制，保留原图，在我的账号范围内更新。
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
