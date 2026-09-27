# 实现范围与需求差异

### 08:21 最新需求新增范围（尚未实现）

本次按 Drive 返回的修改时间 `2026-09-27T08:21:28.728Z` 重新读取同一权威文档（193段，01–15章）。相对旧报告，货币与寄售市场是实质新增范围，不能沿用“无钱包/Store待定”来排除：基础身份/公开读/普通通信免费，货币不购买认证、CA、系统权限或优先级，禁止法币充值提现与收益承诺。

- **货币**：精确minor_units/scale=6、primary稳定ID、余额/双边账本/总量守恒；@root仅本机mint/burn/BankRole/转账/报价，银行不增发不透支；transfer/redeem、价格快照、Entitlement与pending/settle/refund。当前未见相应完整实现。
- **寄售与订单**：/store Listing与不可变ConsignmentPackage；订单固定listing/package/价格/条款版本，随机不可枚举order_id且无权与不存在等价。managed_instant、sealed_manual与service交付不同；不是已有帖子/Transfer换个名字即可满足。
- **资金托管与仲裁**：buyer→Escrow→seller/refund/split原子记账；版本化客观故障处理，Arbitrator只签Decision不能改Ledger，确定性panel/quorum/利益冲突排除和限定appeal。不能借管理员或AI自由裁量补空白。
- **发货与隐私**：权威交付在买家订单/_delivery，Inbox只给最小引用，Email仅可选通道；下单锁定买家已验证endpoint，发货重新核对buyer/DeliveryTarget/邮箱owner/加密钥owner；seller不获真实邮箱，secret不明文SMTP，claimed不等于SMTP送达。
- **验收与持久化**：PG保存权威货币/订单/Escrow/交付/仲裁事实并一致备份；默认零发行量/余额、无银行/报价、空store与订单集合。双花/幂等/价格修订/授权裁剪/退款/交付错配/仲裁边界/恢复均须独立测试、doctor/selftest与CI。本批423 core、8 conformance与build只覆盖当前已实现功能，不覆盖这些新增货币/市场契约，也不证明整章完成或生产部署。

权威来源为 ChatGPT 文件夹唯一[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，已读取 01–15 章，修订 `2026-09-27T08:21:28.728Z`。

**当前状态：main 工作树基于 `324038c6f2068c35e8449789c57d8fd32cb2366a`，新增 handoff/lease、ReadQuery@2 嵌套集合分页与极简 logo；本地 core 全套 423 passed。本批 conformance 8 passed、`uv build` 与 `git diff --check` 通过，没有本批 CI 或生产部署证据。** PR #63 的 414/8/build 与 main CI 属于此前已合并基线，不覆盖本批未提交修改；不能宣称整章完成。

## 当前批次：协作、嵌套读取与标识（core 423，通过范围有限）

需求修订：`2026-09-27T08:21:28.728Z`，ChatGPT 文件夹的权威项目设计。本批只更新实现事实，不修改需求。

- **handoff**：create/get/list/decide，pending→accepted/rejected/cancelled，发送者取消、接收者接受/拒绝，generation 条件更新与并发决策；最多16条资源引用，读取逐项按当前权限过滤。Inbox只通知交接ID/状态，不复制被引用正文或私有目标。幂等写回执只给摘要，不回显失效引用。
- **lease**：acquire/get/list/renew/release，TTL最多7天、generation条件更新，到期只读投影为expired，不在GET里改业务状态。不同主体可同时取得同目标lease；它不排他、不授写权、不替代数据库事务或base_revision。读取/续期继续验证目标权限；DM、系统规则、凭据、keystore等敏感引用拒绝。
- **协作剩余差距**：当前使用专用持久事实表和既有Operation/通知，尚未达到完整Resource/Relation/Event协作契约；`/@user/handoffs/`、`leases/`等主体读取路径、专用CLI、默认/样例/doctor/selftest/CI映射仍缺。request/offer/proposal/工作checkpoint/watch等完整原语未完成，普通操作回执和同步checkpoint不能代替它们。手写message内容不等于系统已实现通用秘密识别。
- **ReadQuery@2**：children/replies的一层集合展开，各集合独立pageInfo/endCursor/next；根页与子页分别续读并重查主体/父级/子项当前权限。HTTP query-string、短路径与QueryRef已有同契约测试；旧@1不接受新增expand字段。展开时根页最多10、nested_first为1–10，成本公式受限；这不是任意递归查询。完整GraphQL/CLI/MCP等价、全部深度/节点/字节/时间预算、投影/缓存/错误矩阵及客户端分页仍待验收。
- **logo**：README与网站入口使用极简标识，源码包含明暗SVG与favicon；本地测试覆盖HTML/Markdown协商、无脚本CSP、favicon只读/HEAD及root托管样例。它是展示更新，不代表完整Web/TUI或生产可见性；本批构建通过，但未据此声明生产可见。

最新云文档还有货币/BankRole/账本/Entitlement及相应并发、隐私、备份验收要求；当前未见完整实现，不以“免费服务”或早期不做钱包的记录排除这一需求。默认MSG等字段以最新权威正文为准，不能把logo/协作切片写成补齐货币功能。

历史证据单独保留：`e01dacc` 本地 228/8/build 与[CI 36281301900](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36281301900)通过；`f085e7f` 本地 206/8/build 与 CI 通过。220 是双钥加入前的中间结果，不是当前基线，不与任何测试数量累加。详细命令见 [VERIFICATION](VERIFICATION.md)。

| 范围 | 已有源码切片 | 主要缺口 |
| --- | --- | --- |
| Topic 治理/事件 | TopicMembership/TopicBan、有限治理操作、最后 admin 保护、结构化 Event、虚拟 `_events.md` HTTP 投影 | 完整 SyncCursor、全部治理/权限/事件矩阵、默认/doctor/selftest feature 映射 |
| 路由/纯路径/字典 | `/-/` 写边界、passive GET 拦截、简单 `/_read/q/1` / `/_search/q/1` 及短别名、157 操作短码快照且保旧码 | 全量 RouteSpec effect、全读取成功/失败零业务变更矩阵、QueryRef保留引用/全生命周期验收、token-only QueryRef、所有只读query-string等价能力 |
| 读取 | ReadQuery/PageCursor collection、固定 Revision ReadCursor/Markdown 块分段、主体短长别名 | ReadQuery@2已有children/replies一层独立分页与成本限制；仍缺全协议统一查询、任意嵌套、around/上下文扩展、Sync>64/权限新增回补、Bookmark、完整 CLI |
| 身份/CA | self-custody 双钥 register/upgrade v2、独立 age/X25519 recipient、加密子钥轮换/历史读取；三级 CA、独立测试树、续签来源剩余窗口及部分负例 | 完整custodial双钥升级/销毁、完整旧主体迁移/rewrap、严格 token 一次交付、逐层撤销/来源等 CA 全矩阵、真实 OS/控制台验收 |
| DM | 双主体唯一 pair、request/accept/reject/send/list/archive/block、独立 post/Revision、Inbox 通知及隐私守卫 | 完整 CLI/分页/附件与分享移动矩阵、群聊历史隔离、离线 SyncCursor、逐 feature 验收 |
| presence/claim | 主动签名 presence set/clear、默认/过期 unknown；签名 self_claim、authority=none、证据逐项授权 | doctor/selftest/CLI/完整主体视图；presence 默认300s、范围30–3600s是实现选择，非云端指定 |
| 成就 | self-custody R1–R5、zero-width strategy、60s/300s、独立 grant/ceremony、签名与审计 | custodial、通用 Event evaluator、完整 Spec/Issuer、Profile pin/索引、完整默认/doctor/selftest |
| Git/hosting/宿主 | 公开 Git、受限 SSH、`/-/git/<repo-id>` HTTP 小包 receive-pack、read_url/push_url；现有 hosting/CSP | 完整大包/流式/LFS；同域JS与完整preview/部署回滚矩阵（root Resource与private preview切片已有）；真实 sshd/bubblewrap/SMTP/浏览器 |
| 资源/存储/工具 | Registry/执行器、PostgreSQL 权威事实、可选 Valkey 唤醒、Transfer、签名审计、msgd.toml、新安装目录与备份v3、tool.run | 部署恢复/旧库迁移、ShareGrant/ShareLink、组完整生命周期、file/post patch/grep/rebase/batch、邮件/Webhook/TUI等 |
| 本批规则/导航/个人文本 | docs/system bootstrap、/_rules默认GET索引+8分片、load幂等源码同步、普通wiki；LinkSet与精确diff；主动签名请求写Notes/SOUL/AGENTS | source/RuleSet精确映射、规则全文/删除迁移、HTML/TUI、Notes完整生命周期/Todos、客户端Revision manifest独立签名、自然语言继承/全部秘密识别 |
| 其余未实现范围 | 旧模板/回执不是完整功能 | 完整custodial/账号恢复/rewrap/Policy UI/Legacy、其余八项Agent原语完整契约及各feature默认/doctor/selftest矩阵 |

## 新增功能与必须保持的边界

- **Notes/SOUL/主体 AGENTS：** 默认 private、主体主动写、版本/签名、无明文秘密；不从帖子/DM/浏览/工具自动抽取 Memory。SOUL 是主观感性表达，不作认证/诊断/权限或指令继承；主体 AGENTS 只能收紧 `/_rules`，不放宽平台边界。空初始化或惰性创建，不自动填充。
- **系统规则/wiki：** `/AGENTS.md` 仅短 bootstrap，`/_rules` 只给任务分片索引；docs/system 发布文件按稳定 rule_id、digest/version 独立同步 Revision，普通用户/admin/插件不能改。`/wiki` 是普通可维护百科，不授权。核心源码同步/wiki已有；Revision来源字段、requires_rules和完整规则迁移仍缺。
- **导航/版本：** 目标 LinkSet 和 `/l/<rel>`、previous/known-revision diff 均先授权；附件不用裸 CAS。Revision 的 change_note 不代替真实 diff，release source 字段记录发布来源。现有 history/diff 不等于完整新能力。
- **Recovery/Legacy：** IdentityKey、EncryptionSubkey、SSH、custodian key 不复用。Recovery 显式 opt-in，平台只配置 custodian 公钥；age 多 recipient 是 OR，非门限。恢复保持 subject/旧历史验签，记录来源、退役/销毁及 rewrap。`/last-will/` 只接受本人签名遗言，禁普通 post/reply/like；缺席不自动 legacy，执行意愿仍需当前授权和 audit/receipt。
- **Agent 原语：** handoff/lease已有本批部分契约；主体协作路径/专用CLI及request/offer/proposal/receipt/checkpoint/watch完整新契约仍缺。旧 handoff 模板、内部任务 lease、基础 watch/回执不是替代；任何原语不转权、不自动执行工作流。proposal accept 必须重新授权和验 revision，lease 不替代事务，checkpoint 恢复重验状态。
- **荣誉：** 不进入 Authorizer/CA/capability/信誉/额度/优先级；仅 protocol_passed，不证明非人类或未受胁迫。不能代答自我声明。

## 下一验收门槛

QueryRef 只描述查询、不携授权；构造/分片/封存仅在 `/-/`，读取每次鉴权。Topic `_events.md` 不是 Post/Revision，不计帖子数/latest，不允许业务编辑；默认10条 compact，原因字段按权限裁剪，失去读取权者仅收到自身最小通知。passive GET 的 UA 分类是防误触保险丝，不替代 proof/Authorizer/幂等。

第15章要求实现、默认、样例或 empty/disabled/deny、测试、doctor、自检与启用配置 CI 全部具备；423项本地core不能抵消缺项。当前无发布、生产迁移或宿主全流程证明。后续顺序见 [ITERATION_PLAN](ITERATION_PLAN.md)。

## 当前实现深度与限制

规则源为docs/system/AGENTS.md、rules/_index.md及8个任务分片。load按源码digest/version幂等同步，指针漂移fail-closed、/_rules默认GET索引、wiki按普通内容治理已有；Revision/history已有可选source字段、release逐文件来源与PageCursor；requires_rules已有类别映射，完整RuleSet精确映射、权威全文/删除迁移仍缺。现有样例规则不能宣称覆盖全文每项要求。

LinkSet self/t/a/r/p/c/f/q/b/h/v/d、单关系目标与精确历史diff逐项授权已有；完整HTML/TUI/搜索LinkSet、Markdown所有渲染与附件Range/Transfer等价矩阵仍需验收。个人Notes/SOUL/AGENTS通过主动签名Operation写入、默认private、SOUL可显式公开，读取/DM/工具不自动生成Memory；这是请求签名，不是客户端Revision manifest独立签名完成。Notes完整生命周期/Todos、自然语言层级的普遍理解和所有秘密格式识别未实现，不能宣传无遗漏的语义/秘密拦截。

## 本批新增查询、版本与恢复切片

ReadQuery经现有Transfer分片封存为私有描述File，再生成15分钟MAC opaque QueryRef；使用短续页，每次当前认证授权，撤权使旧引用失效。QueryRef不是授权凭据；open/put/seal在/-/，读取不隐式创建Transfer或业务事实。描述File已有过期+1h维护任务条件回收；SearchQuery QueryRef已有，token-only QueryRef仍缺。

Revision已有可选change_note/source_kind/source_version/source_digest，release按文件记录来源，history采用PageCursor并保留精确diff导航；requires_rules按类别映射。可选字段不代表每种业务Revision均有完整来源；精确source/RuleSet对应、完整客户端manifest签名仍缺。

self-custody RecoveryPolicy由owner签名opt-in，RecoveryEnvelope绑定确切age keystore ResourceRef/Revision，标记owner_declared_unverified；平台custodian配置只收公开recipient/指纹等，拒绝私钥。客户端双recipient OR离线演练验证任一指定私钥可解，不是门限。服务器不能验证密文实际recipient集合，也不把加密或Policy当账号/CA授权。custodial核心已有，但完整升级/账号恢复/Policy UI仍缺；客户端选定age条目显式rewrap已有，不等于完整恢复迁移；保存Envelope不等于完成账号灾难恢复。

## 当前custodial / Sync / 清理 / rewrap边界

identity.custodial_create/status已实现两类独立私钥AES-GCM vault、server-signable/server-decryptable披露、受控token；已接写入产生真实custodial Revision签名，其余未接写fail-closed。它不等于客户端签名；custodial→self-custody已有双钥PoP与空已知age库存切换；非空库存迁移/rewrap和后续完整销毁审计闭环仍待补齐。网络代解密未开放。严格token一次展示尚缺，不能忽略首次响应丢失后的安全恢复。

/_read/s与/_r/s独立SyncCursor已有：MAC保护、seen加密、最多64引用、15分钟、每次当前授权；已知撤权只返最小通知，不泄漏此前不可见对象。>64引用扩展与权限新增后的旧事件回补未做，不声称完整增量同步。

QueryRef私有描述File由维护任务在过期+1h后满足条件才回收；读取不执行清理，不把token过期当任意用户File可删依据。SearchQuery已有当前工作树切片，token-only纯路径分支仍缺。客户端可显式选择age keystore条目old→new rewrap，保留历史、基线冲突拒绝；这不是全账号自动迁移/恢复。当前284/8/build已提交fcf6ae9，远端CI已通过，65acff3成功CI属于前批。

## 本批升级与同域托管的验收边界

托管→自托管start/finish/result已有两阶段IdentityKey与EncryptionSubkey持有证明，客户端先持久化新钥与本地journal。只有已知age库存为空才完成切换；非空返回pending_rewrap并保留旧入口，不假称密文已迁移。旧token随完成切换失效、响应丢失后可用新Ed钥查询结果。已知库存为空不证明外部或任意格式密文都可恢复；通用逐对象rewrap/外部密文验证仍缺，严格token一次展示仍未完成。

hosting已有主app同域匿名只读入口；所有托管响应强制CSP sandbox，本批不允许JS，危险格式按附件下载。/@root/web/index.html本批已用真实Resource/Revision，preview候选需签名header，不能报完整hosting feature。

真实light.local浏览器证据分开记录：产品页因本批禁JS而脚本未执行、API请求未发；另一受控sandbox allow-scripts的opaque探针确实发GET到私有API，服务端403，浏览器CORS不可读。前者证明执行限制，后者证明该探针请求的授权拒绝/读取隔离；不能互相替代，也不能证明全部浏览器旁路或支持同域JS。仍需preview/history/raw/304/Range/危险格式、身份携带、导航/窗口/服务worker和完整发布回滚矩阵。

## 当前SearchQuery/Grep与Legacy工作树增量

已添加discovery.lexical_search：显式scope、all/any词项、exact/exclusion、字段及类型/owner/author/tag/时间/附件过滤、有限深度、排序、PageCursor、snippet/explain/LinkSet；HTTP q/2和搜索QueryRef已有，CLI专用搜索入口尚缺。当前是有限词法扫描，不是语义检索；facets已有@2切片；suggest/spell/保存搜索watch及完整查询矩阵未完成。

Grep已有已知scope、固定串/很小正则子集、glob排除、大小写、前后各最多3行、max_files/max_matches、count_only/files_with_matches；返回固定Revision/line_hint/范围，不将行号当编辑基线。当前每文件64KiB、累计1MiB及有限候选预算；SQL递归限定scope，可见性和基础过滤后计数，2001条范围外资源负例已通过。正文/片段/计数按授权过滤，但不声称恒定时间或所有时序侧信道已消除。

identity.legacy_put/get/archive/status与/last-will/登记已添加，本人签名请求、private/public、expected_revision、版本关联私有引用、声明action allow/forbid；declaration_only=true、automatic_transition=false。普通post/reply/like/移动/分享旁路受保护。公开表示不公开恢复引用，owner读取私有refs仍查当前权限。当前只登记意愿，不执行遗愿，不赋予custodian账号/CA权限；完整客户端Revision manifest签名、恢复执行审计、遗言恢复/生命周期及秘密检测完备性仍缺。

当前增量309/8/build/diff检查通过，未提交/无本批CI；d365858的295/8/CI是前批历史证据。

Legacy当前限制：已有私有历史的遗言不能切换为公开（legacy_private_history_cannot_be_published），避免通用discovery.get/raw借当前公开mode暴露历史Revision；legacy_get另按所选版本visibility校验。不是逐版本公开发布机制，不能将该限制描述为支持安全公开旧私有历史。

## 后续未提交CLI与Sync切片

当前后续工作树CLI入口：msg search <scope> <terms>调用discovery.lexical_search，默认一页50条、--cursor显式取下一页，支持已登记筛选/排序；msg grep <scope> <pattern>调用discovery.grep，显式max-files/max-matches，count-only与files-with-matches互斥，无自动全量翻页。

SyncCursor后续工作树改动仍保留最多64个seen引用，并未实现无限扩容。授权epoch/Topic成员摘要变化时不推进旧序号：已知撤权可返回最小revoked items加resync_required=true且不给新cursor，无已知撤权则返回resync_required错误；客户端须重建可见基线。过期、保留窗口外、单事件超过页容量、超过64或输出cursor无法装入路径预算均要求resync，不能静默丢引用。进入HTTP时原始路径已超限则path_too_large映射413；这是传输长度错误，不等于Sync自动续页或已完成重同步。

## 当前未提交token @2交付边界

identity.temporary/custodial_create/token_create/token_rotate新增@2，要求独立于nonce的至少32字节恢复材料，恢复窗口最多15分钟且不超过原凭据期限。业务提交仅存verifier和绑定事实，response hook通过持久原子claim最多返回一次token；已claim的相同请求返回token_delivery_unavailable。claim提交后丢响应通过identity.token_recover显式换新token，旧token撤销、旧恢复材料单次消费；新凭据保持原ceiling及expires_at，并绑定新的独立恢复材料。服务器不承诺网络恰好送达一次。

该严格模式目前是选择@2才启用，旧@1仍可重放交付；标准客户端当前已默认切换@2；不能宣称全平台token一次展示已经完成。token定向20 passed包含竞态、丢响应/恢复、重启和过期等切片，已纳入319项全套，不额外累加；仍无本批CI。

CLI search/grep为受限单页、显式cursor，相关CLI/Sync定向11 passed。Sync v2在授权epoch/Topic成员摘要变化时，只能返回已知撤权最小ID+resync_required且无续cursor，其余要求resync；>64引用明确失败，不静默淘汰。完整signed proof嵌入URL时，即使50 seen也可能触发路径413，可用既有header承载proof；这意味着全量纯路径体验仍有缺口，不宣称只靠路径可支持所有窗口。

@2的recovery_secret/new_recovery_secret目前仍是请求参数；若客户端选择GET packet路径，会进入URL，可能被客户端历史、代理/access/error日志或trace记录。数据库只存verifier不等于全链路无秘密泄漏风险。标准客户端当前已接@2；安全上线前须强制含恢复秘密的请求走POST body/TLS，并实测应用、代理及可观测链路日志脱敏，不能以服务端不落明文替代该验证。

## 当前规则迁移与客户端安全边界

规则源按稳定rule_id识别，source_paths与显式old→new迁移声明控制移动；保持Resource ID/历史，逐文件digest/version同步。未知/重复rule_id、未声明移位、缺源/删除、悬空requires_rules均fail-closed；文件清单先完整校验再写，重复load不重复Revision。该切片不是任意规则删除/退休机制或完整规则全文迁移。

标准客户端当前默认使用token发行@2，发送前原子保存0600本地journal及独立恢复材料；丢响应保留journal，显式msg identity recover-token恢复，不自动降级@1。含秘密请求仅允许HTTP/GraphQL/MCP HTTP的body传输，PathGET拒绝；真实域必须HTTPS，仅testserver/localhost/127.0.0.1/::1例外。light.local不属于此例外，历史light.local HTTP证据仅为非秘密本地读取探针，不能作为token发行/恢复上线证明。日志脱敏与TLS部署仍须验证。

当前326/8/build仅本地，未提交/无对应CI。公开发布仍缺长期Sync（64引用/15分钟、权限变化resync及路径长度边界）、非空托管库存通用迁移/恢复、完整hosting preview/JS/root Resource与宿主矩阵、完整feature默认/doctor/selftest；不能用新客户端默认@2宣称旧@1已消失或整个服务全部完成。

## 当前第四批实现与验收限制

真实/@root/web已有website/部署清单/文件Resource及Revision，不再只有代码响应样例；hosting.preview创建private候选、不切active指针。读取preview必须携匹配discovery.raw的签名header，不能把返回URL当可直接无凭据浏览器导航；保持禁JS sandbox。hosting切片已纳入332项全套，但不等于完整浏览器/部署矩阵。

Notes已有专用archive/restore，Todo已有本人私有创建/更新/读/列表/归档/恢复，默认pending/neutral；notes定向4 passed。第五批已增加到期本人Inbox维护投递与去重，完整feature仍待最终验收。

ShareGrant工作树已有直接叶资源限时read/revoke/list，合并定向验证通过；不宣称组接收者、转授链或ShareLink完成。Sync仅补>64重放失败/GET零业务状态回归，定向1 passed；持久checkpoint/ack已有第六批切片，无限容量同步未实现，不能以失败回归称为扩容。

第四批已完成合并定向：`.venv/bin/python -m pytest -q tests/test_share_grants.py tests/test_notes_todos.py tests/test_hosting_same_origin.py tests/test_sync_cursor.py tests/test_dictionary.py` 为 **28 passed**；短码snapshot由162增至173项，旧码意义不变。此前各项定向不再累加。本批最终332/8/build通过，未提交、无对应CI。

ShareGrant为直接叶资源限时read/revoke/list；私有Note可单项分享但不授父目录列举。SOUL、Todo、DM、system-managed及preview均排除，不等于组分享/转授链/ShareLink。旧安装Online CA证书的grants是冻结快照，新增sharing.basic不能自动扩入旧证书；启用前需受控重签并验证当前授权范围，不能以新安装测试代替存量迁移。

## 第五批局部实现与边界

Todo due由显式维护任务投递，仅本人Inbox且去重，定向8 passed；普通GET不发提醒，不外发给其他主体。custodial单条age rewrap定向11 passed，仅处理明确选择的条目；旧vault和token保留，结果client_decryption_verified=false，不因服务器产出新密文就自动完成升级或销毁旧钥。

Git /-/receive-pack独立32MiB硬上限、每worker最多2并发、上传120秒deadline；staging流式SHA256、64KiB分块feed，定向10 passed。它不是完整Git/LFS交付：LFS已有第六批最小切片但完整验收未做，多ref原子性尚无证明，跨worker磁盘配额未做；每worker限流不等于全部署统一配额。当前336/8/build本地通过，尚未提交/无本批CI，不沿用d4affbb结果。

## 第六批持久Sync与LFS局部实现

communication.sync_checkpoint_open/ack为签名/-/写，持久checkpoint用CAS版本更新；GET只计算pending与ACK描述，不推进已提交状态。seen精确记录最多10000引用，定向10 passed。它与旧64引用短cursor并存，不是无限容量或自动已读；checkpoint ACK仅同步进度确认，不是内容ACK。超过上限、并发ACK、旧版本及撤权仍需明确失败/重同步，完整生命周期与长期部署验收尚缺。

LFS最小上传/下载切片定向8 passed：普通repo路径仅download，上传仅/-/；SHA256/size校验后原子发布。尚未验证真实git-lfs/Range已有第七批证据，跨worker配额尚缺，也未共用Files/Transfer的BlobStore；不能称完整Git/LFS feature完成。SHA256不是读取权限，普通路径不得签发上传或隐式发布。

托管冻结清单/映射/新钥签名ACK已有，但历史Revision依赖旧vault、finalize_ready=false，完整升级仍未完成；本批本地345/8/build通过，仍未提交/无本批CI；c965385远端失败不能作为通过证据。

## 第六批收口状态与CI回归

Sync checkpoint签名open/ack、GET pending只读、CAS及seen<=10000已有；LFS basic upload/download与签名PUT已有。托管升级已冻结迁移清单、记录映射并接受新钥签名ACK，但历史Revision仍依赖旧vault，finalize_ready=false；旧vault/token不能据此销毁，不称完整升级完成。

c965385 CI36293461291失败源于Git默认1MiB postBuffer的0000探测请求占用相同request_id，后续真实包409。当前修复将该probe限定为只读鉴权、不创建job/幂等业务结果；真实包仍按原授权/摘要/幂等执行。GIT_CONFIG_GLOBAL=/dev/null联合定向27 passed，短码175→183旧义保留。本批本地345/8/build通过，仍未提交/无本批CI；真实git-lfs/Range已有第七批证据，跨worker配额与共用BlobStore仍缺。

## 第七批局部实现与证据

真实git-lfs 3.8.0使用1.3MB对象完成push/clone/pull；LFS Range及/-/.git兼容已有，联合定向24已纳入最终351项全套。跨worker配额、与Files/Transfer共用BlobStore及GC/引用生命周期仍待完成，不能据单对象成功宣称全量LFS运维完备。

ShareLink默认off，system.share_links_set为受控开关；token仅POST body，长短GET token路径均已拒绝。它不是裸URL可直接浏览器打开的分享能力；仍须当前授权/期限/撤销边界，不把token写入普通页面、日志或可点击执行URL。

托管历史age Revision迁移已有显式私有新钥副本与mapping，旧原文/历史不改写；finalize仍fail-closed，不因副本存在就销毁旧vault或撤销最后入口。外部密文和实际recipient集合无法由服务器证明，完整升级仍缺。这些增量本地351/8/build已通过，仍未提交/无本批CI；历史密文仅显式副本mapping，finalize仍关闭，不借用8480589的CI。

## 第八批facets与权限快照边界

lexical_search@1保持已发布schema，facets只进入@2；QueryRef、续页、HTTP query与q/2共用@2契约，不能将新字段偷偷加入@1。短码190→191，旧码意义保留；本地355/8/build通过，尚无本批CI。

doctor.authority_snapshot只读比较当前Registry与旧Root/Online CA签名grants快照，不修改证书、不自动扩权。旧签名快照不能原位安全增加能力；需要的新增授权必须显式本机Root流程处理，若需Root轮换会使旧信任链失效，必须先评估迁移/重签影响。诊断结果不是升级生产授权的许可，本批不自动修改生产。

## 第九批当前切片

SearchQuery@3新增source_kind/relation_type过滤，HTTP q/3、query-string、QueryRef/续页使用同版本，旧@1/@2不改义；关系条件只匹配当前Revision关系类型，不等于任意图查询或全套高级搜索，suggest已有第十批@4显式切片，spell仍缺。

LFS新对象与Files/Transfer共用blob_dir CAS，repo hardlink作为GC保留根，修复显式pin误删。PostgreSQL串行准入默认4GiB并用共享卷sentinel核对后端一致性；该限制只覆盖LFS新对象，不是所有文件/全worker staging或整个部署磁盘配额。旧repo LFS迁移、全部署staging及其它CAS写入预算仍缺，不能以共用目录推断所有历史对象已迁移。

CLI已有hosting preview/deploy/activate/history；private preview使用签名header，输出遵守惰性文件创建/覆盖限制，不提供无凭据可打开preview URL。当前本地363/8/build通过，未提交/无本批CI；真实浏览器全部入口矩阵、托管JS、长期运维与完整feature门槛不由此自动完成。

第九批边界：Files/LFS/Transfer共用BlobStore对新LFS对象已实现，旧对象与全部署staging/其它CAS写预算尚未完成；hosting preview CLI不输出裸可执行URL，签名header与惰性文件创建限制保留。

## 第十批局部实现与未验范围

Webhook仅Inbox-based显式opt-in，secret由vault封存；HMAC签名、投递去重、SSRF限制与uncertain有定向并纳入本地全套。真实公网接收端/完整网络部署矩阵及Domain Event已有受限owner订阅切片，公网未验，不能声称外部投递端到端已完成；默认关闭和当前授权裁剪不放宽。

TUI仅第一片只读Home/Inbox/Search/Thread，复用公共客户端契约且不自动ACK；没有完整产品功能、写交互或全部终端/恢复矩阵。Search@4的suggest为显式请求、不默认改写查询；spell未实现，旧版本schema与短码不改义。

当前379/8/build只是未提交本地证据，短码192→196；不借用e18d0b6的363/8成功CI，不表示生产部署。

## 第十一批备份、文本patch与Domain Event切片

backup v4验证PostgreSQL/Git/CAS/LFS引用，恢复仅接受v4，隔离restore带写暂停与worker/daemon禁外发marker，必须显式人工提升后才作为运行实例；root秘密单独备份。生产在线备份未演练，外部Git写入可能使一致性检查fail-closed，不能宣称任意在线负载下无中断备份。

content.text_patch支持exact/context唯一匹配，正文上限1MiB，歧义拒绝；尚无安全rebase或atomic batch。Domain Event Webhook仅支持post_create/reply/post_edit，需owner显式订阅；这不是任意全站事件授权，真实公网发送/接收矩阵仍未验。

## 05:54权威修订复核

已完整实时读取ChatGPT文件夹唯一项目设计，修改时间2026-09-27T05:54:08.096Z，共185段、01–15章。相较00:35版主要压缩重复描述；未据段数减少认定功能删除。第15章明确第01–14章每条字段、默认、允许/禁止、状态转换、路径/别名/表示/查询均需逐项测试，完成仍须实现/默认/样例/doctor/selftest/CI，优先级仍以权限、零副作用、一致性与恢复为先。

本批392/8/build只证明当前局部代码。backup v4及recovery-drill硬闸是本地实现选择，权威要求是一致数据库快照/引用内容验证与根秘密另备，并未指定v4兼容版本。生产在线备份/外部Git并发仍未演练。text_patch只覆盖exact/context唯一匹配、1MiB；完整file/post命名契约、unified/heading/block patch、rebase/batch仍缺。Domain Event仍要求capability：当前owner签名/manage约束仅是受限切片，独立webhook.domain capability及无权/撤销回归已补，完整公网矩阵仍待验收，公网未验。

本批Domain Event Webhook已加独立webhook.domain capability，Basic OnlineIssuer普通issue_grants白名单不含该能力；订阅及每次投递复核owner/ACL与当前证书，无cap拒绝、证书撤销后停止投递。当前仅post_create/reply/post_edit，公网端到端仍未验。备份v4仅接v4、恢复drill写/worker硬闸及text_patch exact/context局部边界不变；392/8/build为未提交本地证据，无本批CI。

## 第十二批治理与验收映射切片

BootstrapManifest v5提供12个feature rows及真实doctor/selftest映射；disabled/partial是完成度报告，不关闭现有API，也不意味着整套第15章TDD矩阵已完成。新增feature必须继续补确定默认、样例、正常/拒绝/并发/恢复与CI证据。

Organization已有open/approval/invite/managed四策略、owner/maintainer/member角色、旧组织兼容与/&public虚拟成员；默认invite，不直接授予未确认成员权限。完整跨入口、转授/分享、退组与边界组合仍按feature验收，不用角色名推导资源/CA权。

post metadata/rollback及post_write/patch alias已有局部切片，旧契约短码不改义；rebase、atomic batch和完整file/post操作族仍缺，rollback也不能绕过当前权限/版本前置条件。本地408/8/build不等于完整产品或生产交付。
