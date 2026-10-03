# 实现状态与验收边界

本页列当前技术范围的入口和未完成的验收。具体行为以源码、版本化协议和对应测试为准；旧提交的测试数字不能证明当前版本或生产环境。

| 范围 | 实现与契约 | 仍需验收 |
| --- | --- | --- |
| 资源、执行与授权 | [架构](ARCHITECTURE.md)、[授权来源](AUTHORIZATION_SOURCES.md) | 全部权限来源组合、撤销、跨入口和恢复后的当前授权 |
| HTTP、纯路径与读取 | [协议](PROTOCOLS.md)、[读取入口](READ_ENTRY_ACCEPTANCE.md)、[发布验收](RELEASE_ACCEPTANCE.md) | 完整字段与表示矩阵、深层分页、复杂查询、Sync/resync、GraphQL/CLI/TUI 全路径 |
| 身份、凭据与托管历史 | [一次交付](CREDENTIAL_DELIVERY.md)、[临时升级](TEMPORARY_UPGRADE.md)、[托管历史](CUSTODIAL_HISTORY.md)、[设计变更契约](DESIGN_CHANGE_REQUESTS.md) | 历史无钥主体、全部旧密文、备份密钥退役、恢复响应丢失和秘密日志链 |
| 恢复、Root 与存量数据 | [完整恢复证明](COMPLETE_RECOVERY_PROOF.md)、[恢复说明](TASK_A_RECOVERY_20260928.md)、[Ledger 迁移](LEDGER_MIGRATION.md) | 独立可信的当前撤销检查点、完整重放与受控提升、真实旧库迁移和回滚、物理控制台与 CA 变更 |
| 市场、资金与交付 | [清算与奖励](MARKET_CLEARING.md)、[市场契约](MARKET_CONTRACTS.md)、[托管 checkout](MANAGED_CHECKOUT.md) | 真实生产 schema 演练、外部 SMTP、资金与交付故障矩阵、现场权限和容量 |
| 文件、patch、LFS | [文件与存储验收](RELEASE_ACCEPTANCE.md) | 全生命周期表示、总存储预算、外部 Git 写入及一致备份恢复 |
| 部署 | [部署说明](DEPLOYMENT.md)、[验证范围](VERIFICATION.md) | 目标环境、代理/日志、sshd、worker、备份恢复和外部服务的现场验收 |

这些范围的局部实现不能单独满足项目设计的完整验收要求。需求来源为[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)；功能是否在某个部署上启用，还须以该部署的配置与运行证据判断。
