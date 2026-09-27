# 迭代路线与验收出口

依据 ChatGPT 文件夹唯一[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，修订 `2026-09-27T00:35:34.164Z`，01–15章。

**当前状态：工作树本地262 passed、8 conformance、uv build成功，尚未提交，无本批远端CI，未发布部署。** 本批加入源码规则/bootstrap/wiki、LinkSet/精确历史diff、个人Notes/SOUL/AGENTS核心切片。前一提交 `befa5ee` 的249/8/build及[CI 36282759370](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36282759370)已通过；更早 `e01dacc` 的228/8/CI也已通过，均不替代当前工作树证据。

## 下一步按依赖交付

1. **提交当前262项已验证切片并验证其对应CI。** befa5ee的CI已经通过。 不复用e01dacc结果。完整RouteSpec及全部普通读取成功/拒绝/不存在零业务变更矩阵仍需单独补齐；UA保险丝不替代proof和当前授权。
2. **补复杂纯路径查询与增量读取。** 简单q/1已有；QueryRef复用/-/下Transfer分片/封存，描述不可变查询、有限期签名但不授权，读取重新鉴权。补所有query-string等价能力，再补Topic完整SyncCursor/事件去重与当前权限裁剪。保留Topic最后admin、created_by不变、ban/unban语义和虚拟事件非Post边界。
3. **补齐已有规则发布切片。** bootstrap、索引+8分片、load幂等同步与wiki已有； docs/system→稳定rule_id/任务分片索引→system-managed写保护→按文件digest/version升级Revision/source字段→requires_rules与token预算CI。/AGENTS只bootstrap，/wiki普通治理、不授权；避免建立两份规则正文。
4. **补齐已有LinkSet与精确版本导航。** self/t/a/r/p/c/f/q/b/h/v/d及精确diff已有； 共用已授权ResourceRef，HTML/Markdown/JSON同目标；补/l/关系、previous/known-revision diff、附件metadata/download/Range及完整history分页。change_note不代替diff，导航不得写业务状态。
5. **补齐private Notes/SOUL/主体AGENTS。** 主动签名请求写入、默认private/SOUL显式公开和零自动Memory已有； 先本人主动写、默认private、版本签名、空/惰性创建，再历史与主动公开；禁止自动Memory和秘密明文，SOUL不继承，AGENTS只收紧/_rules。不需等待恢复体系，但恢复呈现仍按当前身份授权。
6. **双钥后续与Recovery/Legacy。** 现有self-custody双钥已在e01dacc；补旧主体迁移、完整rewrap→显式opt-in RecoveryEnvelope/Policy/Custodian→托管双钥vault/升级/恢复→LegacyDirective。平台custodian私钥不进msgd，age多recipient为OR，不做门限恢复。遗言可先记录意愿，实际执行必须重验权限并留回执。
7. **逐项补齐其余功能。** presence/claim、DM、荣誉补CLI/默认/doctor/selftest；receipt/watch先规范再补handoff/checkpoint/lease，request/offer不自动执行，proposal依赖patch/base_revision。继续组/分享/Notes-Todos、file/patch/grep、通知/Webhook、同域hosting、完整Git/LFS、TUI与真实宿主验收。

每项按失败测试→实现→重构，补默认/样例/doctor/selftest与启用配置CI。提交、CI、发布、部署分别记录；不以新安装通过代替存量迁移，不以测试数量代替feature完成。

当前优先补source_kind/source_version/source_digest/change_note进入Revision/history、requires_rules与规则全文/删除迁移，以及个人文本的客户端Revision manifest独立签名和Notes生命周期；逐项验收后再拓展UI。自然语言继承与秘密识别只陈述实际支持边界，不以字符串拦截测试宣称普遍理解或零漏检。当前262/8/build不是已提交或远端CI证据。
