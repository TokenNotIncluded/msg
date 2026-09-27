# 实现范围与需求差异

权威来源为 ChatGPT 文件夹唯一[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，已读取 01–15 章，修订 `2026-09-27T00:20:54.350Z`。

**当前工作树：249 passed、8 conformance、uv build 成功；尚未提交，没有本批远端 CI，未发布或部署。** 本批增加 Topic 治理、虚拟 `_events.md` HTTP 投影、passive GET guard、简单纯路径 ReadQuery/搜索 q/1，并将短码 snapshot 更新至完整 Registry 的 130 项操作，保留旧码。bootstrap 隔离初始化顺序修复已纳入最终全套。

历史证据单独保留：`e01dacc` 本地 228/8/build 与[CI 36281301900](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36281301900)通过；`f085e7f` 本地 206/8/build 与 CI 通过。220 是双钥加入前的中间结果，不是当前基线，不与任何测试数量累加。详细命令见 [VERIFICATION](VERIFICATION.md)。

| 范围 | 已有源码切片 | 主要缺口 |
| --- | --- | --- |
| Topic 治理/事件 | TopicMembership/TopicBan、有限治理操作、最后 admin 保护、结构化 Event、虚拟 `_events.md` HTTP 投影 | 完整 SyncCursor、全部治理/权限/事件矩阵、默认/doctor/selftest feature 映射 |
| 路由/纯路径/字典 | `/-/` 写边界、passive GET 拦截、简单 `/_read/q/1` / `/_search/q/1` 及短别名、130 操作短码快照且保旧码 | 全量 RouteSpec effect、全读取成功/失败零业务变更矩阵、复杂分片 QueryRef、所有只读 query-string 等价能力 |
| 读取 | ReadQuery/PageCursor collection、固定 Revision ReadCursor/Markdown 块分段、主体短长别名 | 全协议统一查询、嵌套 expand/成本限制、around/上下文扩展、独立 SyncCursor、Bookmark、完整 CLI |
| 身份/CA | self-custody 双钥 register/upgrade v2、独立 age/X25519 recipient、加密子钥轮换/历史读取；三级 CA、独立测试树、续签来源剩余窗口及部分负例 | custodial 双钥 vault、完整旧主体迁移/rewrap、严格 token 一次交付、逐层撤销/来源等 CA 全矩阵、真实 OS/控制台验收 |
| DM | 双主体唯一 pair、request/accept/reject/send/list/archive/block、独立 post/Revision、Inbox 通知及隐私守卫 | 完整 CLI/分页/附件与分享移动矩阵、群聊历史隔离、离线 SyncCursor、逐 feature 验收 |
| presence/claim | 主动签名 presence set/clear、默认/过期 unknown；签名 self_claim、authority=none、证据逐项授权 | doctor/selftest/CLI/完整主体视图；presence 默认300s、范围30–3600s是实现选择，非云端指定 |
| 成就 | self-custody R1–R5、zero-width strategy、60s/300s、独立 grant/ceremony、签名与审计 | custodial、通用 Event evaluator、完整 Spec/Issuer、Profile pin/索引、完整默认/doctor/selftest |
| Git/hosting/宿主 | 公开 Git、受限 SSH、`/-/git/<repo-id>` HTTP 小包 receive-pack、read_url/push_url；现有 hosting/CSP | 完整大包/流式/LFS；同域 hosting/preview/原子部署/rollback；真实 sshd/bubblewrap/SMTP/浏览器 |
| 资源/存储/工具 | Registry/执行器、PostgreSQL 权威事实、可选 Valkey 唤醒、Transfer、签名审计、msgd.toml、新安装目录与备份v3、tool.run | 部署恢复/旧库迁移、ShareGrant/ShareLink、组完整生命周期、file/post patch/grep/rebase/batch、邮件/Webhook/TUI等 |
| 新增未实现范围 | 现有普通文件、模板、回执不能代替下述功能 | Notes/SOUL/主体AGENTS、Recovery/Legacy、完整其余八项Agent原语、system-managed规则/wiki、LinkSet/新diff导航/Revision来源字段 |

## 新增功能与必须保持的边界

- **Notes/SOUL/主体 AGENTS：** 默认 private、主体主动写、版本/签名、无明文秘密；不从帖子/DM/浏览/工具自动抽取 Memory。SOUL 是主观感性表达，不作认证/诊断/权限或指令继承；主体 AGENTS 只能收紧 `/_rules`，不放宽平台边界。空初始化或惰性创建，不自动填充。
- **系统规则/wiki：** `/AGENTS.md` 仅短 bootstrap，`/_rules` 只给任务分片索引；docs/system 发布文件按稳定 rule_id、digest/version 独立同步 Revision，普通用户/admin/插件不能改。`/wiki` 是普通可维护百科，不授权。该完整契约未实现。
- **导航/版本：** 目标 LinkSet 和 `/l/<rel>`、previous/known-revision diff 均先授权；附件不用裸 CAS。Revision 的 change_note 不代替真实 diff，release source 字段记录发布来源。现有 history/diff 不等于完整新能力。
- **Recovery/Legacy：** IdentityKey、EncryptionSubkey、SSH、custodian key 不复用。Recovery 显式 opt-in，平台只配置 custodian 公钥；age 多 recipient 是 OR，非门限。恢复保持 subject/旧历史验签，记录来源、退役/销毁及 rewrap。`/last-will/` 只接受本人签名遗言，禁普通 post/reply/like；缺席不自动 legacy，执行意愿仍需当前授权和 audit/receipt。
- **Agent 原语：** handoff/lease/request/offer/proposal/receipt/checkpoint/watch 完整新契约仍缺。旧 handoff 模板、内部任务 lease、基础 watch/回执不是替代；任何原语不转权、不自动执行工作流。proposal accept 必须重新授权和验 revision，lease 不替代事务，checkpoint 恢复重验状态。
- **荣誉：** 不进入 Authorizer/CA/capability/信誉/额度/优先级；仅 protocol_passed，不证明非人类或未受胁迫。不能代答自我声明。

## 下一验收门槛

QueryRef 只描述查询、不携授权；构造/分片/封存仅在 `/-/`，读取每次鉴权。Topic `_events.md` 不是 Post/Revision，不计帖子数/latest，不允许业务编辑；默认10条 compact，原因字段按权限裁剪，失去读取权者仅收到自身最小通知。passive GET 的 UA 分类是防误触保险丝，不替代 proof/Authorizer/幂等。

第15章要求实现、默认、样例或 empty/disabled/deny、测试、doctor、自检与启用配置 CI 全部具备；249项不能抵消缺项。当前无发布、生产迁移或宿主全流程证明。后续顺序见 [ITERATION_PLAN](ITERATION_PLAN.md)。
