# 迭代路线与验收出口

权威[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)最新修订仍为2026-09-27T00:35:34.164Z。

**当前状态：规则源迁移与标准客户端token @2批次，本地326 passed、8 conformance、uv build成功，未提交、无本批CI，未部署。** 前一提交097b252已推送，[CI 36291133946](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36291133946)已completed/success，其本地319/8/build属于前批，不覆盖当前增量。

1. **c62e516的309/8/build与CI已通过；验证后续CLI Search/Grep和SyncCursor改动。** 保持未接custodial写fail-closed；网络代解密不开放。继续补完整RouteSpec/只读零业务变更矩阵，UA guard不替代认证。
2. **补齐custodial非空库存升级。** 双钥PoP/本地journal/空库存切换与新钥查结果已有；下一步显式逐对象可恢复密文rewrap→确认迁移结果→撤销托管token→按策略销毁旧vault钥并审计。每步断线/重复请求可安全恢复，未完成不能冒称self-custody。严格token一次展示必须同时解决首次响应丢失恢复，不靠重复返回秘密掩盖。
3. **扩展Sync和QueryRef边界。** 当前Sync仅15分钟/64个seen引用，补超限明确行为、权限新增旧事件回补、跨协议离线恢复；撤权只最小失效已知引用。QueryRef过期+1h条件回收已有，继续验证引用/并发/重启，补token-only纯路径；所有维护变更仍只在维护任务，不挪到GET。
4. **完善Recovery与Policy生命周期。** 当前自托管opt-in/精确Envelope/客户端OR演练及选定条目rewrap已有；服务器不能证明recipient集合。完整账号恢复、Policy UI、历史钥迁移及Legacy登记已有工作树切片，执行仍独立交付，不把备份密文当账号授权。
5. **补规则、内容与UI。** source/RuleSet精确映射、规则删除迁移、客户端Revision manifest独立签名、requires_rules精度；Notes完整生命周期/Todos、HTML/TUI/搜索LinkSet、完整组分享/patch/grep、通知、其他Agent原语、同域hosting与大包Git/LFS和真实宿主验收。

每项补默认/样例/doctor/selftest/启用配置CI。阶段性的295/8不等于完整feature，也不代表发布、部署或生产迁移。

本批同域hosting已提供匿名只读/禁JS sandbox安全切片；下一轮先把root样例变为真正Resource/Revision并补preview，再在明确需求下评估是否开启JS。浏览器分别验证脚本阻止与实际发起请求的服务端拒绝，不能以opaque探针通过代替产品allow-scripts验收。保持危险格式附件、所有投影同等隔离和完整发布/回滚验证。

SearchQuery共享授权投影/纯路径及长查询、已知范围Grep和Legacy本人签名登记/更新/归档切片已有，但实际执行遗愿留在完整恢复/授权审计之后。搜索须先鉴权再参与计数/聚合/排序/补全，Grep复用当前授权并固定Revision，不能因count_only不返回正文而省略授权。LegacyDirective不产生任何新权限、不依presence或超时自动触发，签署愿望不等于未来执行权。

当前新增查询/遗言切片本地验证通过，后续保持以下回归并补完整feature：搜索与grep测试私有正文/计数/snippet/LinkSet无泄露、QueryRef撤权/过期、排序更新分页及范围外资源对预算/时序影响；已修scope候选与过滤后计数并通过2001范围外回归；更广时序安全仍不宣称完成。Legacy只做声明，不实现自动执行；验本人写、旧revision冲突、公开表示不带私有引用、归档后读取和所有普通互动旁路。保留受限正则和明确输出上限，不为“高级搜索”引入无边界表达式或自动workflow。

Legacy当前限制：已有私有历史的遗言不能切换为公开（legacy_private_history_cannot_be_published），避免通用discovery.get/raw借当前公开mode暴露历史Revision；legacy_get另按所选版本visibility校验。不是逐版本公开发布机制，不能将该限制描述为支持安全公开旧私有历史。

后续CLI按受限单页与显式cursor验收；Sync仍有64引用上限，授权变化要求resync而非宣称自动回补。分别测试数据内resync_required和HTTP超长路径413，未提交改动不沿用c62e516 CI。

## 当前未提交token @2交付边界

identity.temporary/custodial_create/token_create/token_rotate新增@2，要求独立于nonce的至少32字节恢复材料，恢复窗口最多15分钟且不超过原凭据期限。业务提交仅存verifier和绑定事实，response hook通过持久原子claim最多返回一次token；已claim的相同请求返回token_delivery_unavailable。claim提交后丢响应通过identity.token_recover显式换新token，旧token撤销、旧恢复材料单次消费；新凭据保持原ceiling及expires_at，并绑定新的独立恢复材料。服务器不承诺网络恰好送达一次。

该严格模式目前是选择@2才启用，旧@1仍可重放交付；标准客户端当前已默认切换@2；不能宣称全平台token一次展示已经完成。token定向20 passed包含竞态、丢响应/恢复、重启和过期等切片，已纳入319项全套，不额外累加；仍无本批CI。

CLI search/grep为受限单页、显式cursor，相关CLI/Sync定向11 passed。Sync v2在授权epoch/Topic成员摘要变化时，只能返回已知撤权最小ID+resync_required且无续cursor，其余要求resync；>64引用明确失败，不静默淘汰。完整signed proof嵌入URL时，即使50 seen也可能触发路径413，可用既有header承载proof；这意味着全量纯路径体验仍有缺口，不宣称只靠路径可支持所有窗口。

## 当前规则迁移与客户端安全边界

规则源按稳定rule_id识别，source_paths与显式old→new迁移声明控制移动；保持Resource ID/历史，逐文件digest/version同步。未知/重复rule_id、未声明移位、缺源/删除、悬空requires_rules均fail-closed；文件清单先完整校验再写，重复load不重复Revision。该切片不是任意规则删除/退休机制或完整规则全文迁移。

标准客户端当前默认使用token发行@2，发送前原子保存0600本地journal及独立恢复材料；丢响应保留journal，显式msg identity recover-token恢复，不自动降级@1。含秘密请求仅允许HTTP/GraphQL/MCP HTTP的body传输，PathGET拒绝；真实域必须HTTPS，仅testserver/localhost/127.0.0.1/::1例外。light.local不属于此例外，历史light.local HTTP证据仅为非秘密本地读取探针，不能作为token发行/恢复上线证明。日志脱敏与TLS部署仍须验证。

当前326/8/build仅本地，未提交/无对应CI。公开发布仍缺长期Sync（64引用/15分钟、权限变化resync及路径长度边界）、非空托管库存通用迁移/恢复、完整hosting preview/JS/root Resource与宿主矩阵、完整feature默认/doctor/selftest；不能用新客户端默认@2宣称旧@1已消失或整个服务全部完成。
