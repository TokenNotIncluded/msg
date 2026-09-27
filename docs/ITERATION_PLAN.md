# 迭代路线与验收出口

权威[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)最新修订仍为2026-09-27T00:35:34.164Z。

**当前状态：本批SearchQuery/Grep与LegacyDirective本地309 passed、8 conformance、uv build、git diff --check通过；尚未提交，无对应CI，未发布部署。** light.local:18146真实DNS HTTP验证 /、旧search、/_s/q/2、/_search/grep均200，普通POST为405，临时服务已清理。前一提交d365858的295/8/build及CI 36288621652已成功，属于历史证据。

1. **提交本地309项验证通过的SearchQuery/Grep与Legacy切片并验证对应CI。** 保持未接custodial写fail-closed；网络代解密不开放。继续补完整RouteSpec/只读零业务变更矩阵，UA guard不替代认证。
2. **补齐custodial非空库存升级。** 双钥PoP/本地journal/空库存切换与新钥查结果已有；下一步显式逐对象可恢复密文rewrap→确认迁移结果→撤销托管token→按策略销毁旧vault钥并审计。每步断线/重复请求可安全恢复，未完成不能冒称self-custody。严格token一次展示必须同时解决首次响应丢失恢复，不靠重复返回秘密掩盖。
3. **扩展Sync和QueryRef边界。** 当前Sync仅15分钟/64个seen引用，补超限明确行为、权限新增旧事件回补、跨协议离线恢复；撤权只最小失效已知引用。QueryRef过期+1h条件回收已有，继续验证引用/并发/重启，补token-only纯路径；所有维护变更仍只在维护任务，不挪到GET。
4. **完善Recovery与Policy生命周期。** 当前自托管opt-in/精确Envelope/客户端OR演练及选定条目rewrap已有；服务器不能证明recipient集合。完整账号恢复、Policy UI、历史钥迁移及Legacy登记已有工作树切片，执行仍独立交付，不把备份密文当账号授权。
5. **补规则、内容与UI。** source/RuleSet精确映射、规则删除迁移、客户端Revision manifest独立签名、requires_rules精度；Notes完整生命周期/Todos、HTML/TUI/搜索LinkSet、完整组分享/patch/grep、通知、其他Agent原语、同域hosting与大包Git/LFS和真实宿主验收。

每项补默认/样例/doctor/selftest/启用配置CI。阶段性的295/8不等于完整feature，也不代表发布、部署或生产迁移。

本批同域hosting已提供匿名只读/禁JS sandbox安全切片；下一轮先把root样例变为真正Resource/Revision并补preview，再在明确需求下评估是否开启JS。浏览器分别验证脚本阻止与实际发起请求的服务端拒绝，不能以opaque探针通过代替产品allow-scripts验收。保持危险格式附件、所有投影同等隔离和完整发布/回滚验证。

SearchQuery共享授权投影/纯路径及长查询、已知范围Grep和Legacy本人签名登记/更新/归档切片已有，但实际执行遗愿留在完整恢复/授权审计之后。搜索须先鉴权再参与计数/聚合/排序/补全，Grep复用当前授权并固定Revision，不能因count_only不返回正文而省略授权。LegacyDirective不产生任何新权限、不依presence或超时自动触发，签署愿望不等于未来执行权。

当前新增查询/遗言切片本地验证通过，后续保持以下回归并补完整feature：搜索与grep测试私有正文/计数/snippet/LinkSet无泄露、QueryRef撤权/过期、排序更新分页及范围外资源对预算/时序影响；已修scope候选与过滤后计数并通过2001范围外回归；更广时序安全仍不宣称完成。Legacy只做声明，不实现自动执行；验本人写、旧revision冲突、公开表示不带私有引用、归档后读取和所有普通互动旁路。保留受限正则和明确输出上限，不为“高级搜索”引入无边界表达式或自动workflow。

Legacy当前限制：已有私有历史的遗言不能切换为公开（legacy_private_history_cannot_be_published），避免通用discovery.get/raw借当前公开mode暴露历史Revision；legacy_get另按所选版本visibility校验。不是逐版本公开发布机制，不能将该限制描述为支持安全公开旧私有历史。
