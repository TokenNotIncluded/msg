# 验证范围

验证结论必须绑定具体提交、环境和入口。GitHub CI 通过只能证明对应提交的代码检查通过；生产部署仍需现场配置、数据迁移、恢复与外部服务验收。

## 本地与 CI

项目要求 Python 3.15。`tests/` 使用 PostgreSQL；不设置 `MSG_TEST_POSTGRES_URL_TEMPLATE` 时，根目录 `conftest.py` 尝试启动临时 PostgreSQL。CI 在 `.github/workflows/ci.yml` 配置 PostgreSQL、Valkey、age、Nginx、OpenSSL 和 Git LFS，并使用四个测试分片。

```bash
python3.15 -m pip install -e '.[dev]'
python3.15 -m compileall -q src
python3.15 -m pytest tests
python3.15 -m pytest conformance
python3.15 -m build
```

完整 CI 还要求四个分片的 JUnit 测试节点互不重复且覆盖完整收集清单；不能用一组定向测试或旧提交的成功运行代替当前提交的结果。具体命令和环境以 [CI 工作流](../.github/workflows/ci.yml) 为准。

## 部署验收

[部署说明](DEPLOYMENT.md)中的 systemd、Nginx、sshd、worker、备份和恢复路径需要在目标环境验证。尤其要检查真实旧库迁移与回滚、恢复隔离及撤销状态、根材料与文件权限、代理和访问日志中的秘密、真实 SMTP/TLS、外部 Git 写入与容量边界。

恢复预检入口见 [Task A 恢复说明](TASK_A_RECOVERY_20260928.md)，但只读预检不是生产提升许可。功能仍有缺口的范围见 [实现状态](IMPLEMENTATION_STATUS.md) 和各专题契约。没有现场证据时，记录为未验收；不要把单元测试、模拟投递或服务进程存活当成生产证明。
