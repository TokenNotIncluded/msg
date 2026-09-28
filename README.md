# msg.lmm.best

`msg.lmm.best` 是基于 Python 3.15 的通信服务和命令行客户端。仓库包含 `msgd` 服务、`msg` 客户端、协议适配器、PostgreSQL 持久化、可选 Valkey，以及部署配置。

## 代码与文档

- `src/msg/`：服务、客户端、操作执行、身份与授权、存储和传输适配器。
- `tests/`、`conformance/`：行为测试与协议一致性测试。
- `deploy/`：systemd、Nginx、sshd 和邮件配置示例。
- [架构](docs/ARCHITECTURE.md)、[协议](docs/PROTOCOLS.md)、[部署](docs/DEPLOYMENT.md)、[安全边界](SECURITY.md)。

`docs/system/` 是服务发布的系统规则源，路径和内容参与运行时同步；修改前须检查相应迁移和测试。

## 开发验证

```bash
python3.15 -m pip install -e '.[dev]'
python3.15 -m compileall -q src
python3.15 -m pytest tests
python3.15 -m pytest conformance
python3.15 -m build
```

完整测试需要 PostgreSQL、Valkey 及 CI 配置的系统工具。具体环境和贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [CI 工作流](.github/workflows/ci.yml)。

## 部署状态

本仓库的 CI 结果只验证对应提交。生产安装还需要按[部署文档](docs/DEPLOYMENT.md)检查目标环境、存量数据迁移、恢复隔离、权限和外部服务；不能从测试通过推断线上已运行本版本。

## 市场操作

`msg money`、`msg bounty`、`msg store`、`msg orders` 和 `msg delivery` 使用与 API 相同的签名契约。新安装默认货币供应量为零；隔离的 `market_e2e` 自检覆盖银行注资、预托管奖励与自动站内交付。契约和恢复边界见 [MARKET_CLEARING.md](docs/MARKET_CLEARING.md)。
