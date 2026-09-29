# 客户端与服务端安装边界

本页对应 #158 的依赖边界调整，不表示新的协议版本或生产部署已经完成。Python 版本要求仍以 pyproject.toml 为准。

## 安装角色

仅连接远程 msg 服务的客户端，源码安装使用 `python -m pip install .` 或 `uv sync`。安装已经构建的发行文件时，使用 `python -m pip install /path/to/msg_lmm_best-<version>-py3-none-any.whl`。基础 Python 依赖只有 cryptography、httpx、jsonschema、referencing 及它们的传递依赖，不需要安装 Starlette、Uvicorn、psycopg、Valkey、aiohttp、dnspython 或 graphql-core，也不要求本机启动数据库。

完整服务端使用 `python -m pip install '.[server]'` 或 `uv sync --extra server`。发行包安装使用 `python -m pip install 'msg-lmm-best[server]'`，应选用已发布且与服务端部署匹配的版本。此前基础安装包含服务端依赖；升级安装脚本、镜像或虚拟环境时要显式加入 `server` extra，不能继续假定基础安装代表服务端。现有 `.[dev]` 仍拉入完整服务端依赖，原全量 CI 安装路径不变。

`msg`、`msgd` 两个命令入口都保留。`--help` 不要求服务端依赖。客户端环境误运行 `msgd` 的服务命令时，返回 `server_dependencies_required` 和安装提示；不会创建服务目录或偷偷安装依赖。安装 extra 只安装 Python 包，不启动数据库、不部署服务、不授予证书权限。`age`、Git、git-lfs、SSH、bubblewrap 等系统工具仍按具体功能的原要求提供；本次没有无加密、无隔离或空成功的降级实现。

## 共享能力归属

| 能力 | 唯一实现所有者 | 服务端兼容导出 |
| --- | --- | --- |
| OperationResult 的紧凑 wire 表示 | `msg.core.codec.result_wire` | `msg.core.executor.result_wire` |
| 受保护本地文件的原子替换和同步 | `msg.atomic_file.durable_write` | `msg.storage.git.durable_write` |
| 托管升级 PoP 上下文、派生及客户端证明 | `msg.security.custodial_protocol` | `msg.security.vault` 对应名称 |
| 托管迁移决策和 ACK 的签署语句 | `msg.security.custodial_protocol` | `msg.security.custodial_migration` 对应名称 |
| 已发布 MCP 协商版本集合 | `msg.transports.mcp_protocol` | `msg.transports.mcp` 对应名称 |

兼容导出是同一函数或同一常量对象，不复制安全敏感实现。共享协议模块不持有服务器密钥、不接受 Application 或事务，也不验证销毁/退役事实。服务端 vault、权限复核、历史清单、恢复和事务发布继续留在原服务端模块。客户端签名仍绑定原 purpose、主体、请求和上下文；请求版本、签署字节、秘密一次释放、journal 与凭据权限语义不变。

CLI/TUI/签名客户端可以依赖 core 的协议模型、codec、requests、errors，客户端各功能模块、无服务状态的签名/证书/age/托管协议能力、atomic_file 及客户端传输。不能通过执行器、Git/CAS 存储、托管 vault、admin 或 workers 取得公共辅助函数。MCP HTTP 客户端读取版本常量也不再导入服务端适配器；stdio 的原行为与全量协议回归保留。

## 验证边界

`tests/test_client_boundary.py` 检查唯一实现、兼容导出、精确证明上下文、原子替换失败保护和缺失依赖前置错误。`scripts/check_client_install.py` 在全新解释器安装导入拦截器，再加载客户端功能、保存/重读受保护密钥，并通过四个真实传输实现发送同一个签名请求；HTTP 端点由进程内 MockTransport 提供，不访问生产站点。

`.github/workflows/client-boundary.yml` 构建实际 wheel/sdist，在源码目录之外的新虚拟环境中正常安装 wheel 并运行 pip check、两条 help 命令及四传输探针；`--minimal-install` 显式拒绝服务端 Python 依赖。全量 PostgreSQL/Valkey、CLI/MCP、凭据 journal/恢复、协议一致性与构建门禁仍必须通过。该验证不等于 Windows/Android 现场矩阵、外部网络、包体积/性能或生产验收；实际结果及精确提交记录在 CLIENT_BOUNDARY_PROGRESS.md 和对应 PR 中。
