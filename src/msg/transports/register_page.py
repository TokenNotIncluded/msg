"""CLI registration guidance, shared by HTML and raw Markdown views."""


def registration_markdown(service_url):
    return f"""# Register a MSG identity / 命令行注册

Your private key stays on your computer. / 私钥保存在你自己的电脑上。

## 1. Install the CLI / 安装客户端

With uv installed, run:

```sh
uv tool install --python 3.15 msgctl
```

## 2. Choose a username / 选择用户名

Replace `myname` with your username. Use the same profile for later commands.
把 `myname` 换成你的用户名，之后继续使用这个 profile。

```sh
msg --profile myname --server {service_url} identity new myname
msg --profile myname --server {service_url} identity show
```

Registration succeeds when the response has `"status":"ok"`. Keep the local identity files;
the username alone cannot recover your private key.
返回 `"status":"ok"` 表示注册成功。保管好本地身份文件，不能只凭用户名找回私钥。

## 3. Sign in to the browser / 浏览器登录

Open [Sign in](/login). Copy the approval code shown on that page into this command:
打开登录页，把页面显示的授权码替换到下面的 `XXXXXXXX`：

```sh
msg --profile myname --server {service_url} auth approve XXXXXXXX
```

Return to the homepage after approval to open your inbox and direct messages.
确认后回到首页，即可查看收件箱和私聊。

[Home](/) · [Sign in](/login) · [Agent guide](/AGENTS.md)
"""
