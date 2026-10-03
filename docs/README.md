# 文档导航

这里按任务列现行手册。功能是否已在目标服务启用，先读该服务的 `/AGENTS.md`
和操作字典；源码、历史 CI 与生产部署各自需要对应证据。

| 要做的事 | 入口 |
| --- | --- |
| 安装、连接与选择账号 | [安装](CLIENT_INSTALLATION.md)、[连接](CLIENT_CONNECTIONS.md)、[目录和迁移](FILESYSTEM_LAYOUT.md) |
| Agent 接任务和交结果 | [Agent Link 外援接入](AGENT_LINK.md)、[Subagent](SUBAGENTS.md)、[协作编程](AGENT_PROGRAMMING.md)、[跨服务地址](AGENT_INTERNET_ADDRESS.md) |
| 读取、查询与固定版本 | [协议](PROTOCOLS.md)、[字段选择](CLI_FIELD_SELECTION.md)、[查询](SEARCH_QUERY_V5.md)、[SavedQuery](SAVED_QUERIES.md)、[资源地址和事件](RESOURCE_EVENTS.md) |
| 浏览、飞船与发布网页 | [浏览器登录](OAUTH.md)、[星图](post-universe.md)、[飞船](LIVE_FLIGHT.md)、[托管边界](HOSTING_RUNTIME.md) |
| AI 驾驶和游戏通知 | [游客机器人、账号授权与 webhook](ai-game.md)、[可运行示例](../examples/game_bot.py) |
| 保护原身份与恢复账号 | [加密备份](IDENTITY_BACKUP.md)、[备份格式](IDENTITY_BACKUP_FORMAT.md)、[YubiKey](YUBIKEY.md)、[临时升级](TEMPORARY_UPGRADE.md) |
| 检查授权和秘密交付 | [授权来源](AUTHORIZATION_SOURCES.md)、[凭据交付](CREDENTIAL_DELIVERY.md)、[托管历史](CUSTODIAL_HISTORY.md)、[信任路径](TRUST_PATHS.md) |
| 市场、订单与钱 | [市场契约](MARKET_CONTRACTS.md)、[清算](MARKET_CLEARING.md)、[v2 兼容 checkout](MANAGED_CHECKOUT.md)、[offer 迁移](OFFER_RESOURCE_MIGRATION.md) |
| 理解资源与执行边界 | [架构](ARCHITECTURE.md)、[复用](ARCHITECTURE_REUSE.md)、[注册契约](registry-template-tool-contracts.md)、[ToolRunner](TOOL_RUNNER_BOUNDARY.md) |
| 验证、发布与部署 | [本地验证](LOCAL_VERIFICATION.md)、[验证范围](VERIFICATION.md)、[发布验收](RELEASE_ACCEPTANCE.md)、[原生包](NATIVE_PACKAGES.md)、[部署](DEPLOYMENT.md) |
| 恢复服务和迁移旧数据 | [完整恢复证明](COMPLETE_RECOVERY_PROOF.md)、[部分重放与预检](TASK_A_RECOVERY_20260928.md)、[新 Root 导入](NEW_ROOT_LEGACY_IMPORT_RUNBOOK.md)、[Ledger 迁移](LEDGER_MIGRATION.md) |
| 核对验收缺口和历史来源 | [实现与验收边界](IMPLEMENTATION_STATUS.md)、[验收顺序](ITERATION_PLAN.md)、[日期化 issue 台账](ISSUE_RESOLUTION.md)、[历史证据](archive/README.md) |

[知识衰减](KNOWLEDGE_DECAY.md)仍是未实现的设计提案；不能按现有 API 使用。
身份、age、恢复、协议安全和部署手册均保留为现行操作依据。
