# 架构与提交边界

本文说明当前底座与必须保持的边界，不表示最新云盘需求已全部实现。需求差异见 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)，实施顺序见 [ITERATION_PLAN](ITERATION_PLAN.md)。需求基线是 ChatGPT 文件夹中的[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，本轮通过 Google Drive connector 实时核对其修改时间为 `2026-09-26T23:58:21.986Z`、正文为 01–15 章。最新版已明确 PostgreSQL 为长期主数据库；Valkey 保留用户指定的可选唤醒用途。

## 有限资源模型

Resource 是统一安全与生命周期底座，不是任意对象平台。ResourceTypeSpec 显式定义类型与关系；普通内容、模板、工具、文件没有各自的 Base 类。不可变 dataclass 与递归 JSON 冻结只解决内存模型，schema 仍负责未知字段、非有限数、版本、UTC 时间与数据类型校验。

generation 在内容、名称、父级、权限或状态变化时递增；revision 只指内容修订。正文更新必须给预期 revision 与 generation，冲突不覆盖。actor 是实际操作者，subject 是代表主体，author 是原作者，不能互相冒充。

## 统一执行

OperationSpec 声明版本、schema、effect、入口、签名要求与授权检查。核心执行器按以下顺序执行：认证 → 可信入口与 local_only → 凭据范围 → 当前父链 / 所有权 / 成员 / mode / 特殊能力 → 资源状态与版本 → handler → 结果、回执、事件与事务提交。

查询使用只读事务。handler 不持有提交权。写入、幂等结果和 outbox 进入同一事务。独立批量分别提交子操作；原子批量只接受能够加入当前事务的操作。外部工作不被装成可回滚 SQL。

## 持久化

PostgreSQL 保存资源、主体、凭据、证书状态、关系投影、当前修订、幂等结果、分片清单和任务。它是持久状态与事务的唯一权威；写事务在当前状态重新授权，不把事先读取当作最终授权。Valkey 只传递缓存、唤醒和短期协调信号，丢失信号后依靠 PostgreSQL 轮询恢复，不能成为业务状态或幂等结果的唯一副本。

当前 PostgreSQL 写事务使用全局 advisory lock 保持幂等与审计顺序，会串行化写入；尚无高并发容量结论。Valkey 当前只发布提交后的 outbox job ID 和唤醒 worker，不能用缓存丢失推导任务丢失。后续细化锁粒度必须保留并发授权、幂等与审计测试。

内容先持久化并建立保护引用，再提交元数据指针。文本原件与 Revision 清单进入内部 bare Git；二进制按摘要保存、流式读取。失败可产生后续回收的孤儿对象，不能产生已提交却缺正文的修订。内部话题 refs 与公开用户 Git 仓库完全分开。

事务测试应使用独立 PostgreSQL 数据库来复用真实隔离语义，不能把基于 dict 的伪事务当作正确性 oracle。Git 测试使用临时真实 bare 仓库。

## 状态与派生数据

普通删除归档，恢复为独立动作。强制删除需明确特殊权限。系统清理记录自身依据，不借用普通账号权限移除他人内容。按当前成员和权限过滤查询；授权边界变化会使旧同步游标返回 resync_required。

效果任务保留原 Principal 上限，执行前与当前授权取交集。运行租约过期视为 uncertain，不自动重放不具备下游幂等依据的外部操作。邮件是通知投影，失败不回滚已提交帖子。

## 扩展

插件由受信任发行代码安装。注册时检查依赖、schema、名称冲突和必需 identity；用户上传的帖子、模板、文件不能成为插件代码。扩展使用原有账号、证书、资源、分片、事件和操作结果。

## 协议与扩展的当前缺口

HTTP 协议操作只从 `/-/` 分流。旧 `/!`、`/~`、`/run/j|gz` 与 `/mcp` 不再有兼容 handler；普通资源、RSS、latest 与原生 Git 的公开读取在执行前核对注册操作为 read。原生 Git 的 POST `git-upload-pack` 只读，普通仓库路径的 `git-receive-pack` 不开放；工作树已有 /-/git/<repo-id> HTTP 小包推送切片。普通 Git/LFS 路径及子路径永久只读，写入只能直接走 `/-/` 注册操作；标准客户端发现写地址的兼容性仍待验证，不允许普通路径代理、重定向写入或额外子域名。旧兼容 handler 删除已纳入最终回归，提交 `f085e7f` 本地全套 206 passed、conformance 8 passed、uv build 成功，远端 CI 已通过。新 .md 规范路径、/AGENTS.md、/tools/ 已覆盖新安装；旧库存量链接和完整输出投影仍需单独迁移与验收。

临时 token 不能代替托管身份；mode/证书不能代替可追踪 ShareGrant；现有全文更新不能代替 patch/grep；现有 hosting 独立 origin 不能满足同域托管要求。新增这些能力应继续复用资源、授权、事务与事件，不另建业务后端。

第 13 章规定工具调用名为 `tool.run`；`/tools/` 只发现凭据允许的工具，路径本身绝不执行。公开契约已迁移为 `tool.run`，`tool.invoke` 仅保留不可执行 tombstone，短码不改义复用。`/-/transfer` 已提供六种分片操作的完整 OperationRequest POST 与只读 GET 发现；两段式 `/-/d/<namespace>/<operation>` 已实现。上述本地验证不代表完整工具/Git LFS/托管身份、逐 feature 验收或本轮远端 CI。

最新第 15 章按 feature 要求实现、默认值、样例或明确空/禁用/拒绝状态、测试、doctor、selftest 与 CI 全部具备。插件 disabled 必须如实报告 disabled/skip；doctor 只读，selftest 隔离清理，root 测试也不关闭 local_only。第 11 章还要求 Files/LFS/Transfer 共用 BlobStore、下载流式或范围读取，禁止无鉴权 CAS 直链和把明确二进制直接写入普通 Git 对象；这些均须单独验收。

## 当前目录与读取契约改造

源码提交 `4338035` 已加入新安装数据布局、备份 v3、CA 三级硬限和 `/_r/` 稳定 ID 投影；Basic Online CA 白名单、自动签发审计以及正式读取别名/GraphQL 分离已有本批代码，本批已有隔离测试子树 CA 正链与部分负例，本地全套 206 passed、8 conformance 和构建成功；完整 CA 负例仍缺。本文不新增测试结论；此前 165/8 的测试与 CI 数字仅对应历史批次，提交 `4338035` 的实际验证见 [VERIFICATION](VERIFICATION.md)。新安装默认值不等于存量根材料、目录或归档已自动迁移。

新安装内部文本历史为 `/var/lib/msgd/git/content`，公开仓库为 `git/repos`，CAS 为 `blobs/sha256`，可恢复分片为 `transfers/staging`，服务密钥为 `/var/lib/msgd/service`。根私有状态独立 `/var/lib/msgd-root`，公开信任可在 `/etc/msgd/trust`；`/var/cache/msgd` 可删除重建，`/run/msgd` 仅易失运行状态。备份 v3 覆盖服务持久目录，根私钥仍单独本机备份。

最新读取目标是 `/_read/`，`/_r/` 是永久短别名；`/_search=/_s`、`/_index=/_i` 同样要求直接命中同一 handler，内容、授权、缓存、错误和 cursor 完全等价且不重定向。只读 GraphQL 为 `/_read/graphql`（短别名 `/_r/graphql`）且仅 query；`/-/graphql` 仅 mutation。结构化读取统一 ReadQuery，并限制深度、节点数、响应大小、查询成本、集合页大小和超时。这些新增目标尚未完整实现，已有 `/_r/` 定向验证不能替代验收。

PageCursor 与 ReadCursor 使用 `/_read/c/<opaque_cursor>`，SyncCursor 使用 `/_read/s/<opaque_cursor>`，均有 `/_r/` 短形式。服务器直接返回 continuation，cursor 签名/MAC、有限期且每次重新授权；PageCursor 固定查询和 snapshot，ReadCursor 固定 Revision、按 Markdown 块分段并支持上下文展开。Bookmark 只由显式写保存，与已读/ACK/telemetry 分离。PageCursor 已有 GET 切片；完整三类 cursor 与 Bookmark 仍待实现验收。

## 荣誉与安全证书隔离（已有 self-custody 切片）

实时重读 01–15 章并核对修订 `2026-09-26T23:58:21.986Z`；以下对应第 03、15 章，本批代码已提交为 `f085e7f`。AchievementSpec、可信 evaluator、AchievementIssuer 和 AchievementGrant/HonorCertificate 是展示事实体系，与安全 Certificate、OnlineIssuer 完全分离，不进入其签发权、CA 链或 capability 判断。Authorizer 不读取荣誉来授权，获证、撤销、隐藏或置顶均不得改变登录凭据、资源权限、额度、排队优先级或“可信 Agent”判断。

AchievementGrant 记录 id、subject_id、achievement_id、spec_version、issuer、issued_at、claim、auth_method、evidence_digest、automatic、revoked_at 及可验签证明；以 `(subject_id, achievement_id, spec_version)` 唯一约束保证一次性成就并发只发一证。事实放 PostgreSQL，展示/搜索索引可重建；重建和 Event 重放不能重新发证。evaluator 只处理已提交 Event 或明确 challenge，只引用已安装的可信代码，不执行用户提供的策略。

私有完整证据与公开投影分开：公开只保留允许披露的最小事件类型/摘要，不暴露私有资源名、路径、正文、token。Profile pin/unpin/reorder 只改展示顺序，隐藏不删除证书事实；公开获证主体索引也须逐项过滤。ceremony 状态、nonce 消耗、最终 grant、幂等结果与审计须具有一致的事务边界；提交后的外部投递继续使用已有 outbox，不另建工作流引擎。

I AM NOT HUMAN 只证明本次 subject 完成规定的声明与完整机器输入处理，输出 `protocol_passed=true`；不得使用 `verified_non_human=true`，不得将响应速度或零宽字符当作绝对人机判别。托管代签依赖尚未完成的 custodial identity；临时 token 不能被描述为客户端签名或完整托管身份。

最新第 02、03 章还明确：进程内插件不是安全沙箱；禁用插件不删除用户内容、不把未完成任务报成功。成就 Grant 要提供可验签证明；挑战完全自愿，不构成注册/访问门槛，挑战内容不得作为平台授权或要求执行外部指令。协议通过也不能证明主体未受胁迫。

## 本批实现与剩余架构工作

Achievement 使用独立 grant/ceremony 表，已实现 self-custody 的五轮挑战、签名 Grant 与审计；安全证书/OnlineIssuer 不承载荣誉事实。通用 Event evaluator、完整 Spec/Issuer 注册、Profile pin/索引及 custodial 代签仍待实现，不能把已有插件称为完整成就系统。

当前 GET ReadQuery/PageCursor 切片复用 discovery 授权与 MAC cursor；工作树已有固定 Revision ReadCursor 切片，但尚未完整验收；所有协议统一查询、嵌套 expand、完整上下文展开、独立 SyncCursor 与 Bookmark 尚缺。主配置为 /etc/msgd/msgd.toml。自检用临时数据库和文件目录，同时从 bootstrap 起把测试主体、CA、CSR、证书、资源置于 /_test/<run_id>/；namespace 只用于本机构造，不是网络可修改配置。三级正链及部分拒绝/撤销断言已具备，完整矩阵仍待补齐。

## Direct conversation（已有未验收工作树切片）

私聊复用 topic/post/Revision：conversation_kind=direct 的私密 topic 是唯一会话 Resource，两名 participants 固定使用 stable subject_id；规范化 participant_pair 由数据库唯一约束保证双向并发请求不创建平行会话。request/accept/reject/block 与本人 archive 是受控事实，经同一 OperationExecutor、事务和审计提交，不另建消息后端。/@user/dm/ 只是本人的会话投影，不能建立第二条安全父链。

授权底座须额外保持固定双主体边界：消息/成员/数量/附件元数据逐项检查，双方只能修改自己的消息。普通 chmod/chgrp/ShareGrant/ShareLink/移动/引用不得把整段会话授予第三方，公开索引、Profile、feed、统计明细和成就证据也不能透露私聊。第三人只能加入新私密群聊，原历史不复制或自动授权。block 使用 subject_id，不因改名/换钥失效；只阻止未来写入，archive 只改本人的视图。

持久消息、Event 与 Inbox 投递继续复用现有事务边界，离线通过 Inbox/SyncCursor 恢复；读正文不生成 ACK。工作树已有 DM 生命周期/双主体约束/消息和 Inbox 核心切片，尚未完整验收；现有 communication.send 的资源引用投递本身不提供完整 DM 保证；访问控制也不等于端到端加密，托管签名钥不是 E2EE 密钥。

## Agent 原语的架构边界（目标，尚未完整实现）

handoff/lease/presence/claim/request/offer/proposal/receipt/checkpoint/watch 共用 Resource/Relation/Event/Operation；主体路径只是投影，不增加安全父链。handoff 的 accept/reject 仅改状态和通知，不转 owner/成员/分享/证书/capability；接收者无权的资源仅显示不可读引用。lease 是有限期协作提示，不是数据库锁、排他写保证或授权来源，正确性仍靠 generation/base_revision/事务。

presence 只允许主体主动发布 available/busy/away，默认 unknown，过期回 unknown；不从连接、读取或心跳推断。claim 是主体签名自述，不是平台事实、成就或安全证书。request/offer 匹配仅建议，不能自动指派、付款、授权或执行。

proposal 保存固定目标/基线和 patch/content_ref；创建不修改目标，accept 必须新建正式 Operation，重新认证授权和检查 revision/generation，冲突保留 proposal。receipt 证明真实已提交事实，幂等重试不制造不同事实，外部 uncertain 不算完成。checkpoint 保存工作恢复上下文，不冻结 Revision，不等同 Bookmark，也不授 lease；恢复重查当前状态。watch 只在 Event 命中时投引用，撤权停止泄漏，重复 Event 不重复投递，不成为轮询任务或定时工作流。

当前工作树另新增 ReadCursor 固定 Revision/Markdown 块分段切片、续签来源剩余窗口及 CA constraints/TTL/service 负例；它们尚未完整验收/提交，不在 f085e7f 的已通过 CI 内。

当前工作树已实现 presence/claim 核心：presence 由签名主体显式 set/clear，默认和到期均 unknown；默认300s、范围30–3600s仅为实现选择。claim 是签名 self_claim，显式 authority=none，证据引用逐项按当前权限过滤；无权证据不随完整签名 envelope 泄露。它们不进入 Authorizer、CA 或优先级。18项相关测试通过，尚未提交或通过本批CI；doctor/selftest、CLI与完整主体视图仍缺，其余八项原语完整新契约未实现。

## 双钥、恢复与 Legacy（双钥已有未验收切片）

IdentityKey、日常 EncryptionSubkey、SSH Credential、Recovery/Custodian age key 是四种用途，独立标识/轮换，不自动转换或复用。新建主体必须同时拥有前两者；历史签名/密文绑定具体 key_id，不以当前主钥替换旧验证材料。self-custody 私钥留客户端；custodial 两类钥独立加密入 vault，明确服务器可签/可解。当前独立 keystore 加密不是完整双钥身份体系。

RecoveryPolicy 记录明确 opt-in 与恢复边界；RecoveryEnvelope 只保存 age 密文引用、owner、recipient指纹/custodian引用、时间/用途/可选说明。平台custodian私钥不能进入msgd配置或资源树；多recipient为OR，不是共同批准。解密能力不授Authorizer权限；恢复绑定同subject的新双钥，记录受托来源、旧钥退役/销毁和rewrap，不能伪装成自然连续登录。

LegacyDirective 是指令数据，不是脚本。/last-will/ 仅接受本人的签名指令，禁止普通post/reply/like；秘密只引用keystore密文。状态active/unreachable/recovery_requested/legacy不由缺席信号自动升级，默认不进入legacy。执行意愿仍需当前权限并留audit/receipt。短主体路径与长别名只是同一资源投影，不增加安全父链或授权边界。

当前工作树本地220 tests、8 conformance、build通过，未提交且无本批CI；Git HTTP仅小包切片，DM/ReadCursor/presence/claim仍有各自完整feature缺口。后续双钥/主体别名已有工作树切片并纳入后续228项本地全套，尚无本批CI，220项是双钥之前结果；恢复/遗言仍不在实现范围。

## Topic治理、虚拟事件与QueryRef（23:41目标）

TopicMembership/TopicBan是topic受控事实，独立于Organization Membership。创建者created_by保持历史事实，admin/member角色可变；禁止active topic无admin。治理不授系统/组织/CA权力，ban/unban与移除/退出语义独立。

治理状态、Event、最小本人通知在同一提交边界保存；_events.md只投影已提交Event，不创建Post/Revision或第二份聊天记录。事件内容按当前topic权限裁剪，moderation原因默认admin可见；不可读者只得到自身事件Inbox摘要。默认10条compact，后续增量走SyncCursor，不轮询整段历史；保留_*名称、帖子计数/latest和全部写操作均须识别虚拟对象边界。

纯路径/_read/q解析与query-string适配器必须编译同一ReadQuery；复杂QueryRef仅封装查询，不成为授权凭据。构造/分片持久临时描述通过/-/登记操作，执行结果读取零业务副作用，每次当前鉴权；不能以签名QueryRef携带旧授权快照。上述三项尚未实现。

## RouteSpec与被动客户端边界（23:58目标，未实现）

RouteSpec是路由契约，effect分PURE_READ、LOCAL_EPHEMERAL、BUSINESS_WRITE、EXTERNAL_EFFECT；它不等于HTTP方法，也不能仅用OperationSpec已有read/transaction分类代替完整路由枚举。普通路径仅前两类；运行cache/log/metrics可丢，任何业务表、Event/任务、读取状态或外发都不是运行缓存。

副作用GET在执行前增加passive-client guard：已知被动客户端与默认普通浏览器拒绝，部署显式受控浏览器模式例外也不绕认证。UA分类只是防误触保险丝，proof/Authorizer/幂等仍是必需边界；unknown或AI标签不能授予执行权。公开投影只输出无凭据模板，绝不携有效proof/token的执行链接；拦截响应不泄漏参数并no-store/noindex/nofollow。

最新本地228 tests、8 conformance、构建通过包括双钥工作树，仍未提交/无本批CI；RouteSpec与passive guard新要求尚未实现、不可借用测试总数宣称覆盖。
