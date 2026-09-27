# 迭代路线与验收出口

权威[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)最新修订仍为2026-09-27T00:35:34.164Z。

**当前工作树284 passed、8 conformance、uv build通过，未提交、无本批CI。** 前一提交65acff3已推送，CI 36285859198成功，274/8且age实际执行。当前新增custodial双钥vault核心、独立SyncCursor、QueryRef描述条件回收、客户端选定age条目rewrap；不借用前批CI。

1. **提交当前切片并验证对应CI。** 保持未接custodial写fail-closed；网络代解密不开放。继续补完整RouteSpec/只读零业务变更矩阵，UA guard不替代认证。
2. **完成custodial升级闭环。** 新客户端双钥持有证明→显式可恢复密文rewrap→确认迁移结果→撤销托管token→按策略销毁旧vault钥并审计。每步断线/重复请求可安全恢复，未完成不能冒称self-custody。严格token一次展示必须同时解决首次响应丢失恢复，不靠重复返回秘密掩盖。
3. **扩展Sync和QueryRef边界。** 当前Sync仅15分钟/64个seen引用，补超限明确行为、权限新增旧事件回补、跨协议离线恢复；撤权只最小失效已知引用。QueryRef过期+1h条件回收已有，继续验证引用/并发/重启，补SearchQuery和token-only纯路径；所有维护变更仍只在维护任务，不挪到GET。
4. **完善Recovery与Policy生命周期。** 当前自托管opt-in/精确Envelope/客户端OR演练及选定条目rewrap已有；服务器不能证明recipient集合。完整账号恢复、Policy UI、历史钥迁移及Legacy仍独立交付，不把备份密文当账号授权。
5. **补规则、内容与UI。** source/RuleSet精确映射、规则删除迁移、客户端Revision manifest独立签名、requires_rules精度；Notes完整生命周期/Todos、HTML/TUI/搜索LinkSet、完整组分享/patch/grep、通知、其他Agent原语、同域hosting与大包Git/LFS和真实宿主验收。

每项补默认/样例/doctor/selftest/启用配置CI。阶段性的284/8不等于完整feature，也不代表发布、部署或生产迁移。
