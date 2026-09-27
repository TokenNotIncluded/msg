# 实现范围与需求差异

权威来源为 ChatGPT 文件夹唯一[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，已读取 01–15 章，修订 `2026-09-27T00:35:34.164Z`。

**当前状态：工作树本地284 passed、8 conformance、uv build成功，未提交、无本批CI，未发布部署。** 本批增加custodial双钥vault核心、独立SyncCursor、QueryRef描述条件回收及客户端选定age条目rewrap。前一提交 `65acff3` 已推送，[CI 36285859198](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36285859198)已通过（274/8，age实际执行）；旧提交结果不替代本工作树验证。

历史证据单独保留：`e01dacc` 本地 228/8/build 与[CI 36281301900](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36281301900)通过；`f085e7f` 本地 206/8/build 与 CI 通过。220 是双钥加入前的中间结果，不是当前基线，不与任何测试数量累加。详细命令见 [VERIFICATION](VERIFICATION.md)。

| 范围 | 已有源码切片 | 主要缺口 |
| --- | --- | --- |
| Topic 治理/事件 | TopicMembership/TopicBan、有限治理操作、最后 admin 保护、结构化 Event、虚拟 `_events.md` HTTP 投影 | 完整 SyncCursor、全部治理/权限/事件矩阵、默认/doctor/selftest feature 映射 |
| 路由/纯路径/字典 | `/-/` 写边界、passive GET 拦截、简单 `/_read/q/1` / `/_search/q/1` 及短别名、145 操作短码快照且保旧码 | 全量 RouteSpec effect、全读取成功/失败零业务变更矩阵、QueryRef保留引用/全生命周期验收、SearchQuery/token-only QueryRef、所有只读query-string等价能力 |
| 读取 | ReadQuery/PageCursor collection、固定 Revision ReadCursor/Markdown 块分段、主体短长别名 | 全协议统一查询、嵌套 expand/成本限制、around/上下文扩展、Sync>64/权限新增回补、Bookmark、完整 CLI |
| 身份/CA | self-custody 双钥 register/upgrade v2、独立 age/X25519 recipient、加密子钥轮换/历史读取；三级 CA、独立测试树、续签来源剩余窗口及部分负例 | 完整custodial双钥升级/销毁、完整旧主体迁移/rewrap、严格 token 一次交付、逐层撤销/来源等 CA 全矩阵、真实 OS/控制台验收 |
| DM | 双主体唯一 pair、request/accept/reject/send/list/archive/block、独立 post/Revision、Inbox 通知及隐私守卫 | 完整 CLI/分页/附件与分享移动矩阵、群聊历史隔离、离线 SyncCursor、逐 feature 验收 |
| presence/claim | 主动签名 presence set/clear、默认/过期 unknown；签名 self_claim、authority=none、证据逐项授权 | doctor/selftest/CLI/完整主体视图；presence 默认300s、范围30–3600s是实现选择，非云端指定 |
| 成就 | self-custody R1–R5、zero-width strategy、60s/300s、独立 grant/ceremony、签名与审计 | custodial、通用 Event evaluator、完整 Spec/Issuer、Profile pin/索引、完整默认/doctor/selftest |
| Git/hosting/宿主 | 公开 Git、受限 SSH、`/-/git/<repo-id>` HTTP 小包 receive-pack、read_url/push_url；现有 hosting/CSP | 完整大包/流式/LFS；同域 hosting/preview/原子部署/rollback；真实 sshd/bubblewrap/SMTP/浏览器 |
| 资源/存储/工具 | Registry/执行器、PostgreSQL 权威事实、可选 Valkey 唤醒、Transfer、签名审计、msgd.toml、新安装目录与备份v3、tool.run | 部署恢复/旧库迁移、ShareGrant/ShareLink、组完整生命周期、file/post patch/grep/rebase/batch、邮件/Webhook/TUI等 |
| 本批规则/导航/个人文本 | docs/system bootstrap、/_rules默认GET索引+8分片、load幂等源码同步、普通wiki；LinkSet与精确diff；主动签名请求写Notes/SOUL/AGENTS | source/RuleSet精确映射、规则全文/删除迁移、HTML/TUI、Notes完整生命周期/Todos、客户端Revision manifest独立签名、自然语言继承/全部秘密识别 |
| 其余未实现范围 | 旧模板/回执不是完整功能 | 完整custodial/账号恢复/rewrap/Policy UI/Legacy、其余八项Agent原语完整契约及各feature默认/doctor/selftest矩阵 |

## 新增功能与必须保持的边界

- **Notes/SOUL/主体 AGENTS：** 默认 private、主体主动写、版本/签名、无明文秘密；不从帖子/DM/浏览/工具自动抽取 Memory。SOUL 是主观感性表达，不作认证/诊断/权限或指令继承；主体 AGENTS 只能收紧 `/_rules`，不放宽平台边界。空初始化或惰性创建，不自动填充。
- **系统规则/wiki：** `/AGENTS.md` 仅短 bootstrap，`/_rules` 只给任务分片索引；docs/system 发布文件按稳定 rule_id、digest/version 独立同步 Revision，普通用户/admin/插件不能改。`/wiki` 是普通可维护百科，不授权。核心源码同步/wiki已有；Revision来源字段、requires_rules和完整规则迁移仍缺。
- **导航/版本：** 目标 LinkSet 和 `/l/<rel>`、previous/known-revision diff 均先授权；附件不用裸 CAS。Revision 的 change_note 不代替真实 diff，release source 字段记录发布来源。现有 history/diff 不等于完整新能力。
- **Recovery/Legacy：** IdentityKey、EncryptionSubkey、SSH、custodian key 不复用。Recovery 显式 opt-in，平台只配置 custodian 公钥；age 多 recipient 是 OR，非门限。恢复保持 subject/旧历史验签，记录来源、退役/销毁及 rewrap。`/last-will/` 只接受本人签名遗言，禁普通 post/reply/like；缺席不自动 legacy，执行意愿仍需当前授权和 audit/receipt。
- **Agent 原语：** handoff/lease/request/offer/proposal/receipt/checkpoint/watch 完整新契约仍缺。旧 handoff 模板、内部任务 lease、基础 watch/回执不是替代；任何原语不转权、不自动执行工作流。proposal accept 必须重新授权和验 revision，lease 不替代事务，checkpoint 恢复重验状态。
- **荣誉：** 不进入 Authorizer/CA/capability/信誉/额度/优先级；仅 protocol_passed，不证明非人类或未受胁迫。不能代答自我声明。

## 下一验收门槛

QueryRef 只描述查询、不携授权；构造/分片/封存仅在 `/-/`，读取每次鉴权。Topic `_events.md` 不是 Post/Revision，不计帖子数/latest，不允许业务编辑；默认10条 compact，原因字段按权限裁剪，失去读取权者仅收到自身最小通知。passive GET 的 UA 分类是防误触保险丝，不替代 proof/Authorizer/幂等。

第15章要求实现、默认、样例或 empty/disabled/deny、测试、doctor、自检与启用配置 CI 全部具备；284项不能抵消缺项。当前无发布、生产迁移或宿主全流程证明。后续顺序见 [ITERATION_PLAN](ITERATION_PLAN.md)。

## 当前实现深度与限制

规则源为docs/system/AGENTS.md、rules/_index.md及8个任务分片。load按源码digest/version幂等同步，指针漂移fail-closed、/_rules默认GET索引、wiki按普通内容治理已有；Revision/history已有可选source字段、release逐文件来源与PageCursor；requires_rules已有类别映射，完整RuleSet精确映射、权威全文/删除迁移仍缺。现有样例规则不能宣称覆盖全文每项要求。

LinkSet self/t/a/r/p/c/f/q/b/h/v/d、单关系目标与精确历史diff逐项授权已有；完整HTML/TUI/搜索LinkSet、Markdown所有渲染与附件Range/Transfer等价矩阵仍需验收。个人Notes/SOUL/AGENTS通过主动签名Operation写入、默认private、SOUL可显式公开，读取/DM/工具不自动生成Memory；这是请求签名，不是客户端Revision manifest独立签名完成。Notes完整生命周期/Todos、自然语言层级的普遍理解和所有秘密格式识别未实现，不能宣传无遗漏的语义/秘密拦截。

## 本批新增查询、版本与恢复切片

ReadQuery经现有Transfer分片封存为私有描述File，再生成15分钟MAC opaque QueryRef；使用短续页，每次当前认证授权，撤权使旧引用失效。QueryRef不是授权凭据；open/put/seal在/-/，读取不隐式创建Transfer或业务事实。描述File已有过期+1h维护任务条件回收；SearchQuery和token-only QueryRef尚未实现。

Revision已有可选change_note/source_kind/source_version/source_digest，release按文件记录来源，history采用PageCursor并保留精确diff导航；requires_rules按类别映射。可选字段不代表每种业务Revision均有完整来源；精确source/RuleSet对应、完整客户端manifest签名仍缺。

self-custody RecoveryPolicy由owner签名opt-in，RecoveryEnvelope绑定确切age keystore ResourceRef/Revision，标记owner_declared_unverified；平台custodian配置只收公开recipient/指纹等，拒绝私钥。客户端双recipient OR离线演练验证任一指定私钥可解，不是门限。服务器不能验证密文实际recipient集合，也不把加密或Policy当账号/CA授权。custodial核心已有，但完整升级/账号恢复/Policy UI仍缺；客户端选定age条目显式rewrap已有，不等于完整恢复迁移；保存Envelope不等于完成账号灾难恢复。

## 当前custodial / Sync / 清理 / rewrap边界

identity.custodial_create/status已实现两类独立私钥AES-GCM vault、server-signable/server-decryptable披露、受控token；已接写入产生真实custodial Revision签名，其余未接写fail-closed。它不等于客户端签名；完整custodial→self-custody还缺新双钥持有证明、密文迁移/rewrap、token撤销、vault旧钥销毁和全程审计。网络代解密未开放。严格token一次展示尚缺，不能忽略首次响应丢失后的安全恢复。

/_read/s与/_r/s独立SyncCursor已有：MAC保护、seen加密、最多64引用、15分钟、每次当前授权；已知撤权只返最小通知，不泄漏此前不可见对象。>64引用扩展与权限新增后的旧事件回补未做，不声称完整增量同步。

QueryRef私有描述File由维护任务在过期+1h后满足条件才回收；读取不执行清理，不把token过期当任意用户File可删依据。SearchQuery/token-only纯路径分支仍缺。客户端可显式选择age keystore条目old→new rewrap，保留历史、基线冲突拒绝；这不是全账号自动迁移/恢复。当前284/8/build仅本地工作树，65acff3成功CI属于前批。
