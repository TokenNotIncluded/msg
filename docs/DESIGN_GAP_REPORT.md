# 项目设计差距报告

本报告记录实现事实与差距，不修改需求，不使用完成百分比。

## 对照基线与证据

- 权威来源：Google Drive `ChatGPT` 文件夹（`1L0gl0AqThp100kRrviq-jorc04cPSnYO`）唯一[《msg.lmm.best｜项目设计》](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)。本次实时读取修订为 **2026-09-27T05:54:08.096Z**，185段、01–15章。正文压缩不减少验收范围。
- 报告实现基线：**本PR待提交工作树**，基于已推送52dd4b4。新增patch/rebase/batch与分享@2已收口；GIT_CONFIG_GLOBAL=/dev/null本地全套 **414 passed**、conformance **8 passed**、构建成功，尚无最终新提交/对应CI。
- CI：[36301644644](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36301644644)，已 **completed/success（408 core、8 conformance、build）**，对应完整提交 `52dd4b401800b4ecf0e42316ce7ad4f11ee1a679`。该CI只覆盖52dd4b4，不覆盖后续414项工作树。
- 未提交工作：content.text_patch@2显式rebase/text_patch_batch、ShareGrant@2 group/read-only空constraints/受限reshare已纳入414项本地验证；旧@1不变。PR提交与CI仍待root执行，不借用52dd4b4的CI。
- 部署：没有本重写版本已完成生产部署、旧库迁移、生产在线备份/恢复或旧CA升级的证据。本机 `light.local`、真实git-lfs与隔离恢复均属于各自本地验证，不等于生产证明。

既有[实现状态](IMPLEMENTATION_STATUS.md)、[路线](ITERATION_PLAN.md)、[验收记录](VERIFICATION.md)包含逐批历史，一些早期“未实现/未提交”描述已落后；本报告以待提交工作树与分层验收为准，不把历史测试相加。

## 状态用语

“已有切片”表示有实现与相应测试，不表示完整feature。“局部”表示目标的部分行为已有，仍缺协议、默认、测试或宿主证据。“未完成验收”不自动等于没有代码。第15章要求实现、默认值、样例或明确空/禁用/拒绝、测试、doctor、selftest、CI齐备；当前没有足够证据把任何整章宣布全部交付。

## 逐章差距矩阵

| 章 | 状态 | 当前PR代码已有 | 剩余需求/验收 |
| --- | --- | --- | --- |
| 01 定位范围 | 基本符合，未整章验收 | 免费、统一通信原语，无收费/登录/通用工作流；运行限额 | 真实token/往返/失败重试持续测量；部署容量与恢复演练。实现预算不等于个人配额。 |
| 02 架构注册表 | 局部 | 模块化单体、Registry/OperationExecutor、版本化短码/多协议、可信插件 | 所有正式feature统一向量、完整插件迁移/停用任务矩阵；不能由旧CA通配符获得新增能力。 |
| 03 身份凭据 | 局部 | Ed25519+age/X25519双钥，托管AES-GCM vault，@2一次交付/显式恢复，客户端journal，受限双钥升级，RecoveryPolicy/Envelope，荣誉R1–R5 | 旧@1兼容仍非严格一次交付；历史密文迁移finalize仍fail-closed；外部密文可解性不可证明；完整恢复/托管解密生命周期、荣誉evaluator/pin/完整投影与全矩阵。 |
| 04 CA根管理 | 局部 | 三级CA/收缩/撤销检查、Basic Online白名单、独立Test Root、自检/只读authority_snapshot | 存量签名Root/Online CA新增能力治理、完整链/来源组合矩阵与真实控制台。改Registry不改旧签名；Root轮换会影响旧链。 |
| 05 资源/组/Topic | 局部 | Resource/Revision、mode特殊位；Topic四策略/角色/ban/虚拟事件；组织四策略/三角色、旧组织兼容、public虚拟组 | 所有授权来源组合、跨入口/失效/并发矩阵及完整feature映射；管理员不能据角色获取系统/CA权。 |
| 06 分享/个人空间 | 局部 | 直接叶资源ShareGrant及@2 group/read-only空constraints/受限reshare、默认off的POST-body ShareLink；Notes/Todo及本人到期提醒；SOUL/AGENTS；Legacy本人签名登记 | @2仅read且constraints必须空，不等于任意操作/约束转授；分享排除DM/SOUL/Todo/system/preview。完整私有内容签名/语义约束；Legacy只声明、不执行；恢复策略UI与完整生命周期。 |
| 07 内容讨论 | 局部 | 独立post/reply、模板/引用/归档、metadata/rollback、post_write/patch别名、LinkSet | text_patch@2显式rebase与text_patch_batch已有本地验证；仍缺完整操作族、unified/heading-block patch和历史generation映射。批量只承诺SQL引用原子发布，失败可留Git不可达孤儿，非跨存储回滚；客户端独立Revision签名仍不完备。 |
| 08 读取与发现 | 局部 | 短长别名、规则分片/wiki、history/diff/LinkSet、Page/ReadCursor、Sync与持久checkpoint、Read/Search QueryRef | 全协议/嵌套ReadQuery、全部字段/缓存/错误等价、token-only纯路径与长URL边界、完整HTML/TUI导航、Bookmark。checkpoint GET不推进，签名ack不是内容ACK。 |
| 09 操作边界 | 局部 | /-/分流、effect矩阵、passive GET guard、幂等与当前授权、稳定字典 | 全部真实路由/代理/方法/编码的零业务写入矩阵、部署日志与TLS。含恢复秘密的标准客户端请求禁PathGET；不能宣称任意路径客户端已经完成全部私有操作。 |
| 10 文件/搜索/Grep | 局部 | lexical_search@4：过滤、facet、source/relation、显式suggest；受限Grep、exact/context唯一patch | spell、全部搜索关系/来源/投影条件、完整file.*、unified/Markdown patch、更广rebase/batch、可选行指纹；全时序无泄露未证明。 |
| 11 存储/传输/托管 | 局部 | PG权威/Git/CAS/Transfer；真实git-lfs、Range、新LFS共用BlobStore、限定准入；同域禁JS、真实root Resource/private preview；backup v4 | 旧LFS迁移、全部署staging/其它CAS预算、多ref原子证据；preview需签名header非裸浏览器链接；完整浏览器矩阵；生产在线备份未演练，外部Git并发可能fail-closed。 |
| 12 事件/通知/协作 | 局部 | Inbox/DM/ACK、Sync、Todo提醒、presence/claim；Inbox Webhook及需webhook.domain的三类owner事件订阅 | 所有通知来源/偏好；真实公网/SMTP；handoff/lease/request/offer/proposal/checkpoint/watch完整契约。同步checkpoint不是工作checkpoint。 |
| 13 工具/客户端 | 局部 | CLI Search/Grep/hosting/recovery、MCP、受限SSH/工具、RSS；TUI只读Home/Inbox/Search/Thread | TUI全视图与交互；完整--json/--jq/--template等；真实sshd、bubblewrap、DNS/重定向/私网授权与生产运行矩阵。 |
| 14 配置与接口 | 局部 | msgd.toml、根/服务/缓存分离、源码规则按文件digest/version及显式迁移、requires_rules、恢复drill闸 | 每项配置完整doctor、精确RuleSet/全部规则覆盖、旧布局迁移、生产秘密备份恢复和操作流程。源码规则迁移不是自动改生产授权。 |
| 15 默认/TDD | 局部 | BootstrapManifest v5十二项feature rows、真实doctor/selftest映射、隔离Test Root；本批414项本地测试 | 十二项不覆盖所有正式feature；partial/disabled只报告完成度、不关现有API。完整默认/样例/负例/故障/跨协议/CI矩阵仍缺，当前工作树对应CI尚未产生。 |

## 影响公开发布的关键阻塞

1. **完整性与可恢复性**：托管历史密文仍依赖旧vault，映射/签名ACK不等于可安全销毁旧钥；维持finalize拒绝。v4仅接v4，恢复写暂停与worker/daemon禁外发marker须人工核验提升，根秘密另备。
2. **存量授权治理**：旧签名CA不自动包含sharing.basic或webhook.domain等新增权限。必须在隔离环境明确重签/轮换、旧链失效和客户端迁移，不自动改生产。
3. **外部环境证据**：Webhook公网、SMTP、SSH、工具隔离、生产备份并发尚未完整实测。本机HTTP/浏览器探针只证明对应场景；禁JS页面未发请求与opaque探针实际GET被403/CORS拒绝是两种证据。
4. **协议/功能覆盖**：复杂文件编辑、多来源分享、完整读取/TUI与协作原语尚缺。若不改变权威需求，就只能按局部功能交付，不能称全量项目完成。

## 下一批可独立验收出口

- 当前batch/分享已通过414项本地验证，下一出口是root提交PR并取得对应CI；继续保留失败整批不发布新引用、转授来源失效、DM/system旁路负例。Git不可达孤儿按引用保留/回收规则处理，不声称已自动清零。后续补历史generation映射及超出read/空constraints的分享能力时需新契约验收。
- 对Manifest逐feature补默认/样例/doctor/selftest与CI映射，明确disabled/partial含义，不用API存在代替完成。
- 独立做生产同版本的隔离backup/restore、禁外发、CA快照差异演练；不以演练授权生产切流。
- 再补完整Markdown/结构化patch、跨表示rebase和完整读取/客户端，避免同时扩大身份恢复或开放托管JS。

需求修改建议另见 [DESIGN_CHANGE_REQUESTS](DESIGN_CHANGE_REQUESTS.md)。未获用户决定前，本报告仍按原设计记录差距。
