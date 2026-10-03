# 技术验收顺序

本页列剩余工作之间的依赖。每项完成时应以当前提交和实际环境记录测试及运行证据；不沿用历史批次的测试数字。

1. **恢复与存量数据**：取得受保护的真实旧库快照，完成迁移与回滚守恒演练；建立独立可信的当前撤销检查点，验证隔离恢复、完整重放和受控提升。边界见 [恢复说明](TASK_A_RECOVERY_20260928.md) 与 [Ledger 迁移](LEDGER_MIGRATION.md)。
2. **身份与秘密**：关闭历史无钥主体、托管历史密文和备份密钥退役缺口；验证凭据一次释放、再次丢失响应及全入口日志拒绝。边界见 [设计变更契约](DESIGN_CHANGE_REQUESTS.md)、[托管历史](CUSTODIAL_HISTORY.md) 和 [一次交付](CREDENTIAL_DELIVERY.md)。
3. **权限与读写**：完成真实授权来源、撤销后的所有读取入口、嵌套分页、结构化 patch、Revision 签名和 LFS/存储预算矩阵。边界见 [读取入口](READ_ENTRY_ACCEPTANCE.md)、[授权来源](AUTHORIZATION_SOURCES.md) 与 [发布验收](RELEASE_ACCEPTANCE.md)。
4. **市场与外部效果**：在受控环境验证真实 SMTP、资金与交付状态、仲裁、故障恢复及官方端到端市场流程。边界见 [清算与奖励](MARKET_CLEARING.md) 和 [市场契约](MARKET_CONTRACTS.md)。
5. **目标环境验收**：执行当前提交的完整 CI，然后核对目标机配置、代理与访问日志、真实 sshd/worker、容量、备份恢复及运行入口。检查项见 [部署说明](DEPLOYMENT.md) 和 [验证范围](VERIFICATION.md)。

局部测试通过不代表后续依赖已满足；涉及恢复、权限和资金的提升需要相应的现场证据。
