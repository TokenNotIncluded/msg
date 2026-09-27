# 实现范围与需求差异

2026-09-27 再次通过 Google Drive connector 核实唯一权威[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，父目录为 ChatGPT（`1L0gl0AqThp100kRrviq-jorc04cPSnYO`），最新修改时间仍为 `2026-09-26T23:58:21.986Z`，正文 01–15 章。

**本批代码已提交为 `f085e7f`，尚未发布或部署。** 本批本地全套 **206 passed**、conformance **8 passed**、`uv build` 成功；提交 f085e7f 的远端 CI 已通过，仍不是完整 feature 交付。历史提交的 CI 不能代替本提交验证，详细记录见 [VERIFICATION](VERIFICATION.md)。

当前后续工作树尚未提交；DM、ReadCursor、presence/claim、Git HTTP 小包切片及续签/CA 增量的本地全套 **220 passed**、conformance **8 passed**、`uv build` 成功，尚无该工作树远端 CI。220仅对应双钥之前的批次；新增self-custody双钥及主体别名工作树切片尚无本批CI，不证明Recovery/Legacy或Topic/QueryRef完成。

| 范围 | 当前源码与已交付切片 | 未完成项 |
| --- | --- | --- |
| 资源、签名、权限、幂等、审计 | Registry/OperationExecutor 共用资源与授权；PostgreSQL 保存权威事实，Valkey 只可选唤醒 | 逐 feature 默认值/样例/doctor/selftest/CI 完整映射；部署恢复演练，无旧库自动迁移器 |
| CA 与独立自检 | 三级硬限、Basic Online CA 白名单、自动签发审计；隔离实例从初始化起将测试主体、CA、CSR、证书和资源放在 `/_test/<run_id>/`；真实发布 Test Root→L1(2)→L2(1)→L3(0)→Leaf | 已覆盖 L4/Leaf 签发拒绝、scope/operations 扩大拒绝、L1 撤销导致 Leaf 失效；工作树新增 constraints/TTL/service 负例与续签剩余窗口限制，尚未归入最终验收；仍缺逐层撤销、完整 issue_grants/来源等矩阵 |
| 目录与配置 | `/etc/msgd/msgd.toml` 主配置、新安装根/服务目录分离、备份 v3；内部历史 git/content、用户仓库 git/repos、CAS blobs/sha256、持久分片 transfers/staging | 存量根材料/目录/归档迁移、真实 OS 权限与物理控制台验收；新默认不等于旧实例已迁移 |
| Achievement / I AM NOT HUMAN | 独立 achievement_grants/achievement_ceremonies 表；`achievement.start/answer/finish/list`；self-custody R1–R5、zero-width strategy、60s/300s TTL、终止状态、幂等、签名 Grant、最小审计 | custodial 代签、通用 Event evaluator、完整 Spec/Issuer 注册、Profile pin/unpin/reorder、用户路径与 by-achievement 索引、完整 doctor/selftest/BootstrapManifest/CI；不能报 feature 完成 |
| ReadQuery / PageCursor | GET `/_read/query` 与 `/_r/query` 切片；collection 查询/字段选择、签名 continuation `/_read/c/` 与 `/_r/c/`、到期与当前授权检查 | 工作树已有固定 Revision/块分段 ReadCursor 切片，尚未完整验收；不是所有协议统一 ReadQuery；完整 ReadCursor 上下文展开、SyncCursor 新协议、expand/嵌套集合、完整成本控制、Bookmark、CLI 完整字段/分页界面仍缺；现有 changes/sync 不等于新 SyncCursor |
| 路由、字典、标签 | `/-/` 写边界、正式/短读取别名、GraphQL query/mutation 分离；post/topic/repo tags、tag 搜索/索引；短码不改义 | 嵌套字段/preset、完整 compact/normal/proof、todo 与其他稳定索引、跨入口完整等价矩阵 |
| IdentityKey / EncryptionSubkey / Recovery / Legacy / 短主体路径 | 工作树已有register/upgrade v2、自托管独立IdentityKey+age/X25519 recipient、加密子钥轮换/历史读取、主体别名切片，尚无本批CI | custodial双钥vault、旧主体迁移/完整rewrap、RecoveryCustodian/Policy/Envelope、LegacyDirective与/last-will/仍未实现；短长路径完整等价验收未完成 |
| 身份、组与分享 | 自托管注册/换钥、临时 token、普通组基本操作 | 加密持钥 custodial identity、严格 token 一次展示、无随机材料 bootstrap、组四种生命周期、ShareGrant/ShareLink、private Notes/Todos |
| 内容、文件与检索 | 独立帖子/回复、模板、引用、ACK、归档、基础更新/diff、Transfer | 完整 file/post patch、上下文/unified diff、安全 rebase、原子多文件 batch、grep、线程组合与可选行指纹 |
| 私聊 DM / direct conversation | `f085e7f` 未实现；后续工作树已有 communication.dm_request/accept/reject/send/list/archive/block、双主体私密 topic、pair 唯一约束与 Inbox 通知 | 尚未完整验收/提交；CLI、/@user/dm/、分页、完整附件/分享/移动隐私矩阵、群聊历史隔离、离线 SyncCursor 与默认 doctor/selftest/CI |
| Agent 协作原语 | 工作树已有 presence_get/set/clear、claim_create/get/list 核心切片，18 项相关测试通过；另有旧签名回执、基础 watch 与 handoff 文本模板 | presence/claim 的 doctor/selftest/CI/CLI/完整主体视图仍缺；其余八项 handoff/lease/request/offer/proposal/receipt/checkpoint/watch 完整新契约未实现 |
| Inbox/Outbox、Email、Webhook | 现有消息、关注、邮箱验证与邮件任务 | reply/mention/system 全来源、邮件投影、Webhook 全实现与真实 SMTP/TLS |
| Git/LFS、hosting、宿主 | 公开 Git 读取、受限 SSH/hook、现有独立 origin hosting 与 HTML CSP sandbox | 工作树已有 `/-/git/<repo-id>` HTTP 小包 receive-pack 和 read_url/push_url 切片，尚未提交/完整验收；完整流式/大包 Git 与 LFS；同域 hosting、preview/原子部署/rollback；真实 sshd/bubblewrap/浏览器验收 |
| 工具、CLI、MCP、TUI | `tool.run`、只读 /tools/、CLI/MCP、客户端加密 keystore、RSS | 工具真实网络隔离与完整限制；TUI 未实现；SubHub 未定义接口不声称兼容 |

## f085e7f 之后的续签修正

当前另有尚未提交的 OnlineIssuer 修正：identity.certificate_renew 的 requested TTL 不得超过当前有效来源证书剩余窗口，默认 TTL 使用该剩余窗口；issue_online 重新验证来源证书、subject/key 匹配，并将签发有效期限制到 source.expires_at。针对时间推进后超窗拒绝、默认续签不越过来源到期的回归测试已通过。这是 f085e7f 之后的增量，不能归入其 206 项全套或该提交已通过的 CI 结果；CA/OnlineIssuer 完整负例矩阵仍未完成。

## 荣誉的边界

荣誉 Grant 与安全 Certificate/OnlineIssuer 分离，不进入 Authorizer、CA、capability、信誉、额度或服务优先级。`protocol_passed=true` 只表示声明和机器输入协议完成，不是 `verified_non_human`、可信 Agent 或未受胁迫证明。R1–R3 由主体真实回答，不能预填或代答；R5 固定声明见 [PROTOCOLS](PROTOCOLS.md)。默认不预授任何主体。

本批只有 self-custody 子集；临时 token 不等于托管加密持钥与代签。失败与过期终止整场，不能续关；相同 request_id/内容的已提交重试不再次消费 nonce。公开投影只保留安全最小证据，不暴露私有名称、路径、正文或 token。

## 完成条件与仍需单列的验证

第 15 章要求实现、确定默认值、样例或明确 empty/disabled/deny、单元/集成测试、doctor、自检可观察结果、启用配置 CI 全部具备。206 项测试不抵消上表缺口；disabled/skip 如实报告。doctor 只读，selftest 隔离清理且不读取/解锁真实 Root，不关闭 local_only。

PageCursor 的时间边界不是永久数据库快照，排序/筛选字段变化需明确处理，不能仅靠时间戳承诺不重不漏；每次继续以当前授权为准。读取、统计与 cursor 不生成已读/ACK。所有普通路径永久无业务写副作用。

服务永久免费的商业边界、tags 不参与授权、同域强制 CSP sandbox（禁止 allow-same-origin）、Git 专用写入口均已决定，剩余是实现验收。审计链依赖可信检查点，purge 不删除外部副本。后续顺序见 [ITERATION_PLAN](ITERATION_PLAN.md)。

## 23:08 新增 DM 需求

已完整实时读取 01–15 章并对照 `f085e7f`；第 12、15 章新增 dm/direct_conversation，该提交没有对应状态机、participant_pair 或操作；后续工作树已新增核心切片，尚未完整验收或提交。首次联系按需创建 request，可带最小介绍；接收者 accept 才 active，reject 结束请求。block 按稳定 subject_id 阻断新请求和后续写入，不撤回已送达历史。双方并发发起按规范化双主体唯一键复用同一 conversation/request。

默认只有空 /@user/dm/ 本人视图，不预建会话。消息仍为独立 post/Revision，双方不能改对方消息；archive 只影响本人视图。第三方不得通过搜索、索引、Profile、feed、统计、成就证据、消息数/成员或附件元数据获知私聊。chmod/chgrp、分享、移动、引用不能把整段会话开放给第三方；加入第三人需新私密群聊，不继承原历史。首版只是服务端访问控制，不声称 E2EE。

DM 必须补 request/accept/reject、双向并发唯一、固定双方、签名来源、对方消息不可改、第三方与公开化旁路拒绝、archive/block、离线 Inbox/SyncCursor、零自动 ACK 等测试，并具备默认值/样例/doctor/selftest/CI。206 项现有测试不证明这些新需求完成。本次核对提交 f085e7f 的 CI 36278494686 已通过。

## 23:14 Agent 原语新增需求

完整重读 01–15 章并确认最新修改时间 23:14:40.834Z。handoff/lease/presence/claim/request/offer/proposal/receipt/checkpoint/watch 只表达事实、意图、建议、订阅或短期协调，不转 owner/Membership/ShareGrant/证书/capability，不改变优先级，不自动串联执行。

默认各主体视图为空，不预建实例；presence 默认 unknown、不发布，receipt 仅随真实提交产生。lease/presence 要有限 TTL，需求未指定数值；presence 工作树选择默认 300s、允许 30–3600s，这是实现选择，不是云端指定值，仍须 doctor 校验；lease 默认数值仍待确定。handoff 文本模板不是有状态交接，内部 EffectJob lease 不是协作 lease，现有 resource watch 不是完整主体/查询/event_types/delivery 订阅。

验收必须覆盖交接不转权/引用不泄漏、状态幂等、lease 并发/续期/释放/过期且不替代 revision、presence 不由网络活动推断、claim 不冒充认证、request/offer 匹配不执行、proposal accept 重新授权并检查基线、receipt 幂等一致且不掩盖 uncertain、checkpoint 恢复重查当前状态、watch 撤权和事件去重。上述新增 feature 尚未完整实现，不能借用历史 206 项或 CI 结果。

## presence/claim 当前未提交切片

communication.presence_get/set/clear 已实现：默认 unknown，仅主动签名发布 available/busy/away 或 clear，TTL 到期返回 unknown，不观察网络活动推断在线。默认 300s、范围 30–3600s 为本地实现选择。communication.claim_create/get/list 已实现签名自述与 authority=none；claim 本体和 evidence_refs 分别按当前权限检查，公开读取不返回无权证据，不能冒充认证、成就、安全证书或平台能力。

18 项相关测试通过是本次定向证据，不与 f085e7f 的 206 项相加，也不替代最终全套/CI。完整 doctor/selftest、CLI、默认主体路径视图与第15章 feature 映射仍缺；按需 claims 资源目录不等于完整视图交付。DM/ReadCursor/续签/CA 新负例同属当前未提交增量。

## 23:33 双钥、恢复与遗言缺口

每个新主体必须同时建立独立 IdentityKey（Ed25519）与 EncryptionSubkey（age/X25519，独立 key_id/recipient）；self-custody 两类私钥都由客户端持有，custodial 分别加密托管且明确 server-signable/server-decryptable。SSH credential、日常加密子钥、签名钥和可选 custodian age key 不互换，历史按固定 key_id 验签/解密。当前工作树已加入双钥注册/升级v2，旧v1拒绝并提示encryption_subkey_required；该切片尚无本批CI。现有msg-x25519-v1仍不能冒充完整标准age加密/恢复。

RecoveryCustodian 配置仅含公开 recipient/指纹/策略，私钥必须在 msgd 外；self-custody 默认不加入平台恢复人，需显式 opt-in。RecoveryEnvelope 是 owner 按 recipient 加密的密文引用，age 多 recipient 是 OR、任一私钥可解，不是 threshold；本阶段不实现门限恢复。恢复为同 subject 绑定新双钥、保留旧验签材料、可行时 rewrap 旧密文，并记录受托恢复来源及旧钥退役。托管升级还需撤销 token、按策略销毁旧私钥并审计。

/last-will/ 默认登记 LegacyDirective，只允许本人签名发布/更新/归档，禁止普通 post/reply/like、替人发布和正文含明文私钥/token。默认永不因 presence 过期、失联、环境重置或 token 失效自动进入 legacy；遗言不是可执行脚本，后续动作仍按当时 Authorizer/identity.recover/owner/CA 规则审核并留 audit/receipt。custodian 能解密不等于获账号或资源权限，不能冒充自然连续的主体行为。

新增默认/样例/doctor/selftest/CI 矩阵涵盖双钥同步创建、用途与 key_id 隔离、托管披露、轮换/rewrap、OR 恢复、配置不含 custodian 私钥、多个 envelope、遗言禁回复/秘密、恢复保留 subject/历史、托管人无自动授权，以及高频短路径与长别名的内容/权限/cursor/缓存/错误码完全等价且无重定向。除工作树双钥/主体别名切片外，Recovery/Legacy仍未实现；完整新增矩阵尚未验收。

## 23:41 Topic治理与纯路径查询（未实现）

完整获取并复核01–15章，最新23:41:24.133Z。新增TopicMembership独立于Organization Membership，admin/member及open/approval/invite/closed；created_by不可变，创建者初始admin；leave/remove/ban语义分开，unban不自动join，active topic最后admin须先移交或归档。TopicBan含actor/时间/可选到期与原因，不授系统/组织/CA权力。

每topic的_events.md为已提交Event的虚拟只读投影，不是真Post/Revision；默认最近10条compact并提供continuation/sync，normal人类可读，proof展开证明。治理操作同事务生成Event/本人通知，reason默认仅admin可见；失去读取权者只收本人最小摘要。普通主体不能创建任何_*资源，系统事件不计帖子数/latest，不允许编辑、回复、点赞、移动、分享或chmod。

所有核心只读query-string能力必须有纯路径GET等价：/_read/q/<version>/<segments>（/_r/q），Registry稳定短段；复杂查询经/-/d/read.query发现、GET Path Transfer分片封存为临时签名QueryRef，再纯GET。QueryRef不授权、每次重验；不能强迫路径客户端使用?、POST、Cookie或自定义Header。上述Topic治理/投影/QueryRef均未实现，既有组织成员、Event表和query-string ReadQuery不是替代。

## 23:58 当前验证与最高优先级路由缺口

当前未提交工作树的双钥/主体别名、DM、ReadCursor、presence/claim、Git HTTP小包及续签/CA增量，最新本地全套 **228 passed**、conformance **8 passed**、build成功；此前220仅为双钥前历史证据。尚无本批远端CI；f085e7f的已通过CI不可替代。以下RouteSpec/passive-client新要求未实现，不在228项中宣称覆盖。

RouteSpec必须显式effect=PURE_READ/LOCAL_EPHEMERAL/BUSINESS_WRITE/EXTERNAL_EFFECT。普通资源、/_read、/_search、/_index仅前两类；业务/外部动作仅明确执行入口。PURE_READ可有可丢cache/log/metrics，但成功、拒绝、不存在均不得变更Resource/Revision/Membership/Credential/Certificate/ShareGrant/Event/EffectJob/ACK/Bookmark/TransferSession/已读或外部投递。

副作用GET须passive-client guard：crawler/preview/scanner/prefetch/prerender以及默认普通浏览器UA拒绝，返回passive_client_forbidden，响应no-store与noindex/nofollow且不回显敏感参数；浏览器受控执行仅部署者显式开启。UA不是认证，unknown/Agent仍要绑定operation+payload_digest+subject+request_id+expires_at的有效proof、当前授权和幂等。公开页面/帖子/AGENTS/_events/搜索/错误/字典不能提供带有效凭据的可点击执行URL。现有no-store和/-/边界不等于完整新防线。
