# 本地验收记录

## 结论

本地已实际执行 **101 项测试**，另执行 **5 项完整分片传输验收**：HTTP、纯路径 GET、MCP Streamable HTTP、CLI、MCP stdio。共 **106 个独立用例通过**，这些已执行用例中没有失败或跳过。

这不是“全部发布验收通过”：当前容器是 **Python 3.13.5**，没有 Python 3.15、graphql-core、tiktoken、bubblewrap 或 OpenSSH 服务。对应运行时、GraphQL、实际 tokenizer 与宿主隔离验收**没有执行**。项目仍要求 Python >=3.15，生产网络入口未为了测试改成允许 3.13。

准确环境、依赖版本、用例名称和对应 JUnit 文件见 [summary.json](verification/summary.json)。本次没有推送 GitHub、运行远程 Actions、创建 PR、发布 release 或更新线上实例。

## 实际执行的分组

| 报告 | 用例数 | 范围 |
| --- | ---: | --- |
| core.xml | 31 | 模型、事务、身份 / 签名、授权、业务执行、配置校验 |
| capabilities.xml | 18 | 每项特殊能力的证书、操作、scope、撤销 / 过期边界 |
| services.xml | 24 | 批量、扩展、HTTP、分页、诊断、备份、SSH/Git guard、根轮换 |
| tools.xml | 18 | 网络策略、工具限定、幂等任务、撤销、执行后的不确定状态 |
| exchange.xml | 5 | 分片、真实 CLI / stdio 子进程、客户端加密密钥库 |
| client-basic.xml | 2 | 客户端重试与上传正文发布 |
| client-http.xml / client-path.xml / client-mcp.xml | 3 | 三种客户端的跨传输恢复 |
| transport-matrix.xml | 3 | HTTP、GET 路径、远程 MCP 的相同分片流程 |
| cli-matrix.xml / stdio-matrix.xml | 2 | CLI 与本地 MCP 的相同分片流程 |

全部报告在 [verification/](verification/)。受当前命令执行时限影响，测试分组执行并按用例去重核对，不宣称做过单进程全套运行。相同测试没有重复计入总数。某次整体调用超时并不作为通过依据；客户端用例随后分别执行完成并取得独立 JUnit 结果。

## 测试使用的真实组件

SQLite 事务、回滚与版本更新；临时 bare Git 对象、引用和实际 Git reference-transaction hook；Ed25519 与 PIN 封装；HTTP 回环套接字；真正的 msg CLI / MCP stdio 子进程；客户端加密、下载解密；备份导出与恢复。

工具授权和任务状态使用受控 runner fixture，不假装执行了真实 bubblewrap。SSH 测试执行了受限命令解析、账号凭据验证和真实 Git hook，但没有实际 sshd 登录握手。根管理测试调用隔离安装内部用例，不把测试 fixture 当成绕过本机控制台检查的生产入口。

## 尚未完成的发布门槛

执行 `python3.15 -m pytest conformance`。其中运行时依赖检查、真实 GraphQL 分片和 tokenizer 预算测试不使用 importorskip 或空成功；缺少依赖会失败。新 CI 配置将这些设为必须执行的步骤，但本次没有向远程推送，所以没有 CI 成功链接。

真实 OpenSSH 登录 / 禁转发、bubblewrap 文件系统隔离、公网与重定向请求、SMTP/TLS 投递以及物理控制台操作还需按 [部署文档](DEPLOYMENT.md) 验收。设计中尚未完成的完整邮件事件投影、浏览 / 下载统计、任意初始化损坏自动修复等见 [实现范围](IMPLEMENTATION_STATUS.md)。

## 构建与静态检查

构建结果和安装烟雾测试记录在 `verification/build.log` 与 `verification/package-smoke.json`。源代码与 conformance 已通过 `compileall`，部署 shell 通过 `sh -n`。没有运行 mypy、ruff、独立安全审计或压力基准，因此不提供相应通过率、覆盖率或吞吐量声明。
