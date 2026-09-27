# 迭代路线与验收出口

依据 ChatGPT 文件夹唯一[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，修订 `2026-09-27T00:35:34.164Z`，01–15章。

**当前状态：工作树本地274 passed、8 conformance、uv build成功，尚未提交，无对应远端CI，未发布部署。** 本批加入ReadQuery QueryRef、Revision来源/history分页、requires_rules类别映射及自托管Recovery Policy/Envelope。前一提交 `8fdfb85` 的262/8/build与CI已通过；更早befa5ee的249/8/CI属于历史证据，均不代替当前工作树验证。

## 下一步按依赖交付

1. **提交当前274项已验证切片并验证其对应CI。** 8fdfb85的CI已经通过。 不复用e01dacc结果。完整RouteSpec及全部普通读取成功/拒绝/不存在零业务变更矩阵仍需单独补齐；UA保险丝不替代proof和当前授权。
2. **补复杂纯路径查询与增量读取。** 简单q/1与ReadQuery QueryRef已有；先补描述File生命周期/自动回收、SearchQuery和token-only纯路径认证，再补所有query-string等价能力、Topic完整SyncCursor/事件去重与当前权限裁剪。QueryRef只描述查询，不授读取权；保留Topic最后admin、created_by不变、ban/unban语义和虚拟事件非Post边界。
3. **补齐已有规则发布切片。** bootstrap、索引+8分片、load幂等同步与wiki已有； docs/system→稳定rule_id/任务分片索引→system-managed写保护→按文件digest/version升级Revision/source字段→requires_rules与token预算CI。/AGENTS只bootstrap，/wiki普通治理、不授权；避免建立两份规则正文。
4. **补齐已有LinkSet与精确版本导航。** self/t/a/r/p/c/f/q/b/h/v/d、精确diff和最小history分页已有；继续补HTML/TUI/搜索结果同目标投影、附件metadata/download/Range、完整history来源字段与归档关系验收。change_note不代替diff，导航不得写业务状态。
5. **补齐private Notes/SOUL/主体AGENTS。** 主动签名请求写入、默认private/SOUL显式公开和零自动Memory已有；继续补Notes完整归档/删除/分享生命周期、客户端独立Revision manifest签名、秘密格式防漏与主体AGENTS规则继承的可审查边界。SOUL不继承，AGENTS只收紧/_rules；恢复呈现仍按当前身份授权。
6. **双钥后续与Recovery/Legacy。** self-custody双钥及签名opt-in Policy/Envelope、公开平台Custodian配置和离线OR解密演练已有；下一步补旧主体迁移、明确对象的旧密文rewrap、完整Policy/Envelope生命周期，再做托管双钥vault/升级/账号恢复和LegacyDirective。平台custodian私钥不进msgd，不做门限恢复；遗言执行须重验权限并留回执。
7. **逐项补齐其余功能。** presence/claim、DM、荣誉补CLI/默认/doctor/selftest；receipt/watch先规范再补handoff/checkpoint/lease，request/offer不自动执行，proposal依赖patch/base_revision。继续组/分享/Notes-Todos、file/patch/grep、通知/Webhook、同域hosting、完整Git/LFS、TUI与真实宿主验收。

每项按失败测试→实现→重构，补默认/样例/doctor/selftest与启用配置CI。提交、CI、发布、部署分别记录；不以新安装通过代替存量迁移，不以测试数量代替feature完成。

当前已有Revision可选source字段/history分页和requires_rules类别映射；优先补精确source/RuleSet映射、规则全文/删除迁移，以及个人文本的客户端Revision manifest独立签名和Notes生命周期；逐项验收后再拓展UI。自然语言继承与秘密识别只陈述实际支持边界，不以字符串拦截测试宣称普遍理解或零漏检。当前274/8/build不是已提交或远端CI证据。

本批QueryRef和自托管恢复核心已经形成：先提交验证，再补描述File生命周期/可恢复清理、SearchQuery与token-only认证，保持读取零写入。Recovery先完成实际可解性演练与Policy生命周期，再做明确授权的账号双钥恢复、旧密文rewrap与custodial；不得把owner声明当服务器验证的recipient事实。source/RuleSet精确映射和完整manifest签名仍独立验收，不能因已有可选字段宣称完备。
