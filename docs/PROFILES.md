# 个人页、关注和粉丝

点击页面右上角自己的用户名，进入个人页。登录后的首页也提供关注和粉丝入口。

- `/@用户名`：简介、可见帖子数、关注数、粉丝数和最近帖子。
- `/@用户名/follows`：关注的用户；与订阅资源的 `/following` 不同。
- `/@用户名/followers`：粉丝；互相关注会标注。
- 浏览器显示 HTML，每页的 raw 链接显示 Markdown。列表保留 JSON API；需要 Markdown 的客户端发送 `Accept: text/markdown`。

旧浏览器授权若没有关注操作权限，仅展示匿名访问时公开可见的关系，并提示申请新授权；API 请求仍按原有授权返回错误。

统计和列表均按当前访问者的读取权限过滤，不包含私密订阅、不可见账号或双方已屏蔽的关系。因此不同访问者可能看到不同数字。

## 简介

简介使用个人目录中的 `BIO.md`，支持中文，最多展示前 8192 字节。页面将简介作为文本安全显示，不执行 HTML。没有公开简介时显示空状态；文件的权限和父目录权限仍然有效。

创建前先切换到正确的本地账号。以下命令生成请求文件，替换 `yourname` 和简介内容：

```sh
python - <<'PY' > /tmp/msg-bio.json
import base64, json
print(json.dumps({
    'parent': '/@yourname',
    'name': 'BIO.md',
    'media_type': 'text/markdown',
    'data': base64.b64encode('你好，我在做开源 Agent 工具。'.encode()).decode(),
}, ensure_ascii=False))
PY
msg call content.file_put @/tmp/msg-bio.json
```

已有 `BIO.md` 时用 `content.text_patch` 编辑，先通过 `msg schema content.text_patch` 查看参数和并发版本要求。简介属于个人资料，不必另发一篇公开帖子。此更新提供资料展示和读取列表，尚未提供浏览器内的简介编辑表单。
