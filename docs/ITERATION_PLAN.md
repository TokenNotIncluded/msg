# 迭代路线与验收出口

权威[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)最新修订仍为2026-09-27T00:35:34.164Z。

**当前状态：工作树本地295 passed、8 conformance、uv build成功，未提交、无本批CI，未发布部署。** 本批增加托管转自托管的受限双钥升级闭环及同域只读hosting安全切片。前一提交 `fcf6ae9` 的284/8/build与[CI 36287082964](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36287082964)已通过，不替代本批验证。

1. **提交当前295项切片并验证对应CI。** 保持未接custodial写fail-closed；网络代解密不开放。继续补完整RouteSpec/只读零业务变更矩阵，UA guard不替代认证。
2. **补齐custodial非空库存升级。** 双钥PoP/本地journal/空库存切换与新钥查结果已有；下一步显式逐对象可恢复密文rewrap→确认迁移结果→撤销托管token→按策略销毁旧vault钥并审计。每步断线/重复请求可安全恢复，未完成不能冒称self-custody。严格token一次展示必须同时解决首次响应丢失恢复，不靠重复返回秘密掩盖。
3. **扩展Sync和QueryRef边界。** 当前Sync仅15分钟/64个seen引用，补超限明确行为、权限新增旧事件回补、跨协议离线恢复；撤权只最小失效已知引用。QueryRef过期+1h条件回收已有，继续验证引用/并发/重启，补SearchQuery和token-only纯路径；所有维护变更仍只在维护任务，不挪到GET。
4. **完善Recovery与Policy生命周期。** 当前自托管opt-in/精确Envelope/客户端OR演练及选定条目rewrap已有；服务器不能证明recipient集合。完整账号恢复、Policy UI、历史钥迁移及Legacy仍独立交付，不把备份密文当账号授权。
5. **补规则、内容与UI。** source/RuleSet精确映射、规则删除迁移、客户端Revision manifest独立签名、requires_rules精度；Notes完整生命周期/Todos、HTML/TUI/搜索LinkSet、完整组分享/patch/grep、通知、其他Agent原语、同域hosting与大包Git/LFS和真实宿主验收。

每项补默认/样例/doctor/selftest/启用配置CI。阶段性的295/8不等于完整feature，也不代表发布、部署或生产迁移。

本批同域hosting已提供匿名只读/禁JS sandbox安全切片；下一轮先把root样例变为真正Resource/Revision并补preview，再在明确需求下评估是否开启JS。浏览器分别验证脚本阻止与实际发起请求的服务端拒绝，不能以opaque探针通过代替产品allow-scripts验收。保持危险格式附件、所有投影同等隔离和完整发布/回滚验证。
