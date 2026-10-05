# 验证范围

验证结论必须绑定具体提交、环境和入口。GitHub CI 通过只能证明对应提交的代码检查通过；生产部署仍需现场配置、数据迁移、恢复与外部服务验收。

## 本地与 CI

项目要求 Python 3.15。数据库测试不设置 `MSG_TEST_POSTGRES_URL_TEMPLATE` 时，根目录 `conftest.py` 或自检尝试启动临时 PostgreSQL；无需数据库的测试不会启动集群。CI 在 `.github/workflows/ci.yml` 配置 PostgreSQL、Valkey、age、Nginx、OpenSSL 和 Git LFS，并使用八个测试分片。

`pytest -m "not db"` 运行无需真实 PostgreSQL 的测试；其中也包含真实文件与子进程的集成检查。
`pytest -m db` 显式运行真实 PostgreSQL 测试；fixture 的传递依赖自动标记，动态后端在 fixture 参数声明，测试内自建集群使用 `postgres_required` 声明，不额外启动集群。

```bash
uv sync --locked --extra dev --python 3.15
uv run --locked --extra dev ruff check . --output-format json
uv run --locked --extra dev ruff format --check .
uv run --locked --extra dev python -W error -m compileall -q src tests conformance scripts
uv run --locked --extra dev python -m pytest tests
uv run --locked --extra dev python -m pytest conformance
uv run --locked --extra dev python -m build
uv run --locked --extra dev python scripts/check_package_artifacts.py dist
```

完整 CI 还要求八个分片的 JUnit 测试节点互不重复且覆盖完整收集清单；不能用一组定向测试或旧提交的成功运行代替当前提交的结果。具体命令和环境以 [CI 工作流](../.github/workflows/ci.yml) 为准。

安装包的独立演练见 [deployment-rehearsal 工作流](../.github/workflows/deployment-rehearsal.yml)。
它从同一 uv.lock 导出哈希依赖锁，分别验收 client-only/server 安装和正式 selftest，
并运行真实回环 daemon 与 OpenSSH/Git。逐 issue 的当前入口和现场材料见
[发布验收与交接](RELEASE_ACCEPTANCE.md)，不能从隔离演练推导生产已切流。

## UI 审计

`scripts/check_ui_audit.py` 使用真实渲染函数和内存 HTTP fixture，不连接 PostgreSQL。
共享首页、文档、搜索、登录、`/now` 与 `/terminal` 检查深浅配色，Root 按其设计仅检查深色；
四个视口为 1280×800、768×1024、390×844、320×640，手机启用触摸，所有页面减少动态效果。
依据 [DESIGN.md](../DESIGN.md) 与 [.impeccable/surfaces](../.impeccable/surfaces/)，
可见文字至少 11px，交互命中区域至少 44×44px，正文对比度至少 4.5:1（大字 3:1），
页面无横向溢出、未捕获脚本异常及未豁免的 `console.error`。只有正文中的行内链接免除命中区域检查。

```bash
mkdir -p ~/.cache/msg-test-tmp/ui-audit
TMPDIR=~/.cache/msg-test-tmp/ui-audit uv run --extra server --with playwright python scripts/check_ui_audit.py --require-browser
TMPDIR=~/.cache/msg-test-tmp/ui-audit uv run --extra server --with playwright python scripts/check_ui_audit.py --pages home now root --widths 1280 390 --verbose --shots ~/.cache/msg-ui/ui-audit/shots
rm -rf ~/.cache/msg-test-tmp/ui-audit
```

`MSG_BROWSER_PATH` 或 `--browser` 选择浏览器，缺省 `/usr/bin/chromium`，启动参数包含 `--no-sandbox`。
`--extra server` 提供真实渲染函数所需的 Starlette、Markdown 等依赖；默认客户端依赖不足以渲染这些页面。
缺少浏览器或 Playwright 时打印 SKIP 并成功退出；`--require-browser` 将缺失视为失败。
默认只保存结构化报告到 `~/.cache/msg-ui/ui-audit/report.json`，`--report` 可指定路径，截图须显式传 `--shots DIR`。
`--verbose --limit N` 打印各类问题的前 N 条元素、文字和实测数值。

豁免写入 `scripts/ui_audit_allow.json`：每条明确页面、检查类别、具体元素选择器
（异常用固定前缀的 `message_pattern`）、原因，可再限定视口与主题。不得用白名单掩盖新发现的生产缺陷。
Root 内存 fixture 没有飞行 WebSocket 服务，因此只豁免回环 `/_flight` 的特定握手错误；
其余异常继续失败。审计是渲染回归检查，不证明生产部署、真实飞行服务或物理手机验收。

## 部署验收

[部署说明](DEPLOYMENT.md)中的 systemd、Nginx、sshd、worker、备份和恢复路径需要在目标环境验证。尤其要检查真实旧库迁移与回滚、恢复隔离及撤销状态、根材料与文件权限、代理和访问日志中的秘密、真实 SMTP/TLS、外部 Git 写入与容量边界。

恢复预检入口见 [Task A 恢复说明](TASK_A_RECOVERY_20260928.md)，但只读预检不是生产提升许可。功能仍有缺口的范围见 [实现状态](IMPLEMENTATION_STATUS.md) 和各专题契约。没有现场证据时，记录为未验收；不要把单元测试、模拟投递或服务进程存活当成生产证明。

## 面向 agent 的 MCP

`tests/test_mcp_tools.py` 检查有限目录、无凭据参数、精简投影和用户名映射。
`tests/test_mcp_oauth.py` 使用真实 PostgreSQL 与现有登录流程检查受众绑定、PKCE、
权限范围、刷新和撤销；`tests/test_mcp_direct.py` 检查新私信授权版本及旧签名版本兼容。
`tests/test_mcp_agent.py` 从 HTTP 入口检查默认目录、连接提示、身份、私信、回复、
读取不 ACK、重试和重启幂等。高级操作兼容仍由 HTTP、stdio 和 Agent Link 测试覆盖。

发布后再次读取 OAuth 元数据和默认 MCP 目录，确认不存在完整 envelope 字段。
协议验收与 ChatGPT 宿主实际完成授权是两个证据范围；不能用服务端 HTTP 测试替代
宿主保存、刷新 token 和真实回调验收。
