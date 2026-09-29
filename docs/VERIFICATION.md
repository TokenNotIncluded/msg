# 验证范围

验证结论必须绑定具体提交、环境和入口。GitHub CI 通过只能证明对应提交的代码检查通过；生产部署仍需现场配置、数据迁移、恢复与外部服务验收。

## 本地与 CI

项目要求 Python 3.15。`tests/` 使用 PostgreSQL；不设置 `MSG_TEST_POSTGRES_URL_TEMPLATE` 时，根目录 `conftest.py` 尝试启动临时 PostgreSQL。CI 在 `.github/workflows/ci.yml` 配置 PostgreSQL、Valkey、age、Nginx、OpenSSL 和 Git LFS，并使用八个测试分片。

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

## 部署验收

[部署说明](DEPLOYMENT.md)中的 systemd、Nginx、sshd、worker、备份和恢复路径需要在目标环境验证。尤其要检查真实旧库迁移与回滚、恢复隔离及撤销状态、根材料与文件权限、代理和访问日志中的秘密、真实 SMTP/TLS、外部 Git 写入与容量边界。

恢复预检入口见 [Task A 恢复说明](TASK_A_RECOVERY_20260928.md)，但只读预检不是生产提升许可。功能仍有缺口的范围见 [实现状态](IMPLEMENTATION_STATUS.md) 和各专题契约。没有现场证据时，记录为未验收；不要把单元测试、模拟投递或服务进程存活当成生产证明。
