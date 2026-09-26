# 实现范围与需求差异

2026-09-27 对照[权威需求文档](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)核对当前源码。需求基线是 ChatGPT 文件夹中的[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，本轮通过 Google Drive connector 实时核对其修改时间为 `2026-09-26T22:19:27.354Z`、正文为 01–15 章。最新版已将 PostgreSQL 写入长期主数据库基线；Valkey 保留用户明确决定的可选唤醒用途，不保存唯一业务事实。`provenance.json` 是旧重写来源记录，不能证明已覆盖最新需求；仓库没有该记录所指的完整设计快照。

**当前是已有核心实现、正在补齐新需求的版本，不是完整需求交付，也未部署线上。** 下表“源码已有”只说明存在实现；本地与 CI 通过范围见 [VERIFICATION](VERIFICATION.md)，真实宿主与完整功能验收仍须分别完成。

本轮路由清理、工具契约更名、分片与字典入口对齐后，Python 3.15 本地全套为 **165 passed**（89.20s），conformance 为 **8 passed**（31.47s），`uv build` 再次成功。此前定向检查已包含其中，不额外累加。提交 `4c4b377` 的[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36274644099)已通过：Python 3.15、PostgreSQL 16 与 Valkey，165 passed（175.58s）、8 conformance passed（44.22s），sdist/wheel 构建成功。这不代表逐 feature 完成或线上部署。阶段 1 仍有明确缺口，下表只列已实现或实际通过的范围。验收重点是一次原子操作在各入口的资源、授权、幂等与错误一致，以及 /-/ 外无副作用，不以增加接口数量代替能力交付。

| 范围 | 当前源码与边界 | 下一步 |
| --- | --- | --- |
| 资源、修订、关系、签名、证书、权限、幂等、审计 | core/、security/ 与插件已有实现；注册表统一操作 | 补齐新能力的同一授权契约和逐 feature 验收 |
| PostgreSQL、Valkey | PostgreSQL 保存权威元数据与持久任务；Valkey 可选唤醒；本地测试及提交 `4c4b377` 的远端 CI 通过 | 部署备份恢复演练、旧数据迁移方案；无旧库自动迁移器 |
| 内容、回复、模板、引用、ACK、归档 | plugins/content.py、discussion.py 已有基本流程 | 独立线程读取、最新操作名与完整 patch/rollback 契约需补齐 |
| 自托管与临时身份 | 注册、临时 token、轮换、升级、签名密钥与委托已存在 | 临时 token 不等于服务器加密持钥的 custodial identity；托管密钥库与代签来源未实现 |
| 组 | 已有创建、成员增删、admin 管理 | open/approval/invite/managed 与 owner/maintainer/member 完整生命周期未实现 |
| GET-only token/bootstrap（已通过本地全套） | 字典短码标量写、显式 request_id/expiry、bootstrap、轮换、expected 前置条件已实现；已纳入 165 项最终全套；单值最多 4096 UTF-8 字节，仍受默认 8192 字节原始路径上限约束 | 仅短码/标量；无随机材料 bootstrap、复杂嵌套输入与托管身份未完成；token 幂等重放可再次交付，与严格一次展示要求不一致；URL 日志边界还需部署验收 |
| HTTP / GET / GraphQL / MCP / CLI | /-/p、签名 /-/g、/-/graphql、/-/mcp 已接入；旧 `/!`、`/~`、`/run/j|gz` 与 `/mcp` 兼容 handler 已删除并完成定向验证 | 本轮全套覆盖路由零副作用回归；`/-/transfer` 已提供六操作完整 OperationRequest POST 与只读 GET 发现；标量短码已扩展 token/bootstrap 写，嵌套字段/preset 与完整跨入口验收仍待补齐 |
| 短码字典与 schema | Registry 生成最小目录与分级详情、ETag；稳定短码快照拒绝改义/保留废弃码，支持两段式 namespace/operation 查询；字典测试已纳入最终全套 | 顶层 enum/const 已支持，嵌套字段与 preset 尚未覆盖；快照尚未对外发布 |
| 规范资源与发现 | 新安装 /AGENTS.md、真实 msg-entry 技能与 /tools/ 已纳入最终全套验证，不再 seed /rules；新 post/reply 用 .md，无后缀旧式 URL 经授权只读 308 到已有 .md Post；私有资源不泄露跳转 | 旧库真正无后缀 Post 的改名/别名迁移未做；完整技能与局部规则发现、compact/normal/proof 完整投影尚缺 |
| 文件、检索与编辑 | 统一分片上传下载、文件资源、基础全文更新及 diff | file.* 操作族、上下文/unified diff、安全 rebase、grep 与可选行指纹尚未形成完整实现 |
| 分享与个人空间 | 现有 mode/证书授权不能代替 ShareGrant | ShareGrant/ShareLink、private Notes/Todos 及本人到期提醒未实现 |
| Inbox/Outbox、关注、邮件 | communication 插件、邮箱验证、communication.send 邮件任务存在 | reply/mention/system 全来源投递及完整邮件事件投影未完成；无真实 SMTP/TLS 验收 |
| Webhook | 未见 endpoint/subscription/delivery 完整实现 | 默认关闭、当前授权过滤、签名、重试、uncertain 及只走 /-/ 的管理入口 |
| Git、LFS | 原生公开 Git 读取、受限 SSH push 与 hook 存在 | 普通仓库及 LFS 路径、全部子路径禁止 push/上传/管理 handler，永久只读已确定，写入只能直接走 `/-/` 注册操作；无完整 LFS；`/-/git/<repo-id>` push_url 与 read_url 输出待实现及真实验收 |
| 网页托管 | hosting.create/deploy/activate 与独立 origin 实现存在；HTML 的 GET/HEAD/304 已加 CSP sandbox，允许脚本但禁止 allow-same-origin | 同域路由、web.preview 与完整 patch→preview→deploy→rollback 验收仍缺 |
| 工具、SSH、RSS、密钥库 | 有受控工具 worker、受限命令与 Git hook、RSS、客户端加密密钥库 | 第 13 章规范调用名为 `tool.run`，已完成公开契约更名，`tool.invoke` 仅保留不可执行 tombstone；完整 GET 标量工具参数仍须验收；`/tools/` 及子路径只读发现，参数或方法也不能触发执行。ToolSpec 的名称/说明、版本/摘要、输入输出/并发上限及大输入输出 Transfer 引用须逐项验收。真实 bubblewrap、sshd 与公网/重定向验收未做；SubHub 未定义的接口不声称兼容 |
| TUI | CLI 存在，未见 msg tui 命令 | 待公共契约稳定后实现；不直连数据库，不新增 /login |
| 初始化、doctor/selftest | 默认资源、隔离诊断与恢复已有实现 | BootstrapManifest 缺逐 feature 的默认值、sample、doctor_check、selftest_case 完整映射；不能宣称全部功能验收 |

## 完成条件

最新第 15 章按 feature 验收：实现、确定默认值、样例或明确 empty/disabled/deny、单元/集成测试、doctor、selftest 可观察结果、CI 缺一不可。BootstrapManifest 与测试须共用 feature_id、enabled_by_default、default_config、sample_resource、doctor_check、selftest_case。通过某批测试不能替代整项完成；插件 disabled 应明确 reported disabled/skip，不能伪报 pass。doctor 不改生产状态，selftest 在隔离范围执行并清理，测试 root 也不能关闭 local_only。

最新 PostgreSQL 选型仍要求 FakeMetadataStore 与实际后端运行同一 MetadataStore/MetadataSession 契约：实际 PostgreSQL 后端仍须验证事务、并发、恢复与内容引用一致性，不能据选型变更免除要求。当前没有以本批路由或工具更名宣布这些 feature 完成。

## 保留的产品决定与边界

最新版已删除旧第 16 章待决清单：tags、永久免费的商业边界、Git push URL、托管 CSP sandbox、稳定 ID 投影和 telemetry 均已作出决定，不能继续标为产品待定。tags 只作元数据，不参与授权；商业付费机制明确禁止；Git 写地址为 `/-/git/<repo-id>`；托管 HTML 强制 sandbox，脚本最多 allow-scripts，禁止 allow-same-origin；GET-only 投影为 `/_r/<resource_id>/json|meta|raw|history|rev/<revision_id>`；浏览/下载统计只能异步 telemetry，不改变 Resource、Revision、generation、已读或 ACK。上述新增能力仍待实现和验证。

根私钥仅本机管理，当前内部测试不等于真实物理控制台验收。recover 用于可信根材料恢复，rotation --resume 用于轮换日志恢复；任意初始化损坏自动补全并非已验证能力。审计摘要链依赖可信检查点，不能阻止完整写盘权限者重算历史；purge 不抹去已导出的副本。

实施顺序、依赖和可验收出口见 [ITERATION_PLAN](ITERATION_PLAN.md)。

## 22:19 修订新增实施缺口

- CA：链推导 L1/L2/L3，Root 下最多三级 CA；子深度上限 2/1/0，Leaf 无签发权。三级硬限代码已加入，仍须等待本轮全套与完整链验收。Basic Online CA 为 L1、深度 0，固定最小 issue_grants，禁止 ca_only、system.*、resource.purge、tool.net.private、group.manage_override。
- 目录：根私有状态 `/var/lib/msgd-root/`（root:root 0700，私钥 0600）；公开根证书可复制到 `/etc/msgd/trust/root.crt`。持久状态 `/var/lib/msgd/` 下区分 git/content、git/repos、blobs/sha256、transfers/staging；可重建缓存 `/var/cache/msgd/`，易失运行状态 `/run/msgd/`。需迁移与权限验收，不自动搬动现有根材料。
- 存储：msgd 不触碰 PostgreSQL 物理数据目录；内容引用只保存 backend + stable key/content_ref，不以宿主绝对路径作业务字段，不扫描 Git/CAS 推断授权状态。
- doctor/selftest：doctor 检查 CA 链、key_id、issuer、scope、issue_grants、TTL、深度、撤销与在线 CA 白名单；selftest 仅在 `/_test/<run_id>/` 使用独立 Test Root 构造 L1→L2→L3→Leaf，不能读取/解锁真实根。必须验证逐项收缩、撤销级联、Leaf 无签发权与 L4 拒绝。既有 165 项测试不证明这些新增验收已完成。

## 当前开发批次与 22:19 读取扩展

当前开发批次已加入新安装数据布局、备份 v3、CA 三级硬限和 `/_r/` 稳定 ID 投影；Basic Online CA 白名单、自动签发审计以及正式读取别名/GraphQL 分离已有本批代码，完整自检与最终回归仍在推进。最终全套尚未完成，此前提交的测试/CI 数字仅为历史证据，不证明本批完成。新安装默认值不等于存量根材料、目录或归档已自动迁移。

最新读取目标是 `/_read/`，`/_r/` 是永久短别名；`/_search=/_s`、`/_index=/_i` 同样要求直接命中同一 handler，内容、授权、缓存、错误和 cursor 完全等价且不重定向。只读 GraphQL 为 `/_read/graphql`（短别名 `/_r/graphql`）且仅 query；`/-/graphql` 仅 mutation。结构化读取统一 ReadQuery，并限制深度、节点数、响应大小、查询成本、集合页大小和超时。这些新增目标尚未完整实现，已有 `/_r/` 定向验证不能替代验收。

PageCursor 与 ReadCursor 使用 `/_read/c/<opaque_cursor>`，SyncCursor 使用 `/_read/s/<opaque_cursor>`，均有 `/_r/` 短形式。服务器直接返回 continuation，cursor 签名/MAC、有限期且每次重新授权；PageCursor 固定查询和 snapshot，ReadCursor 固定 Revision、按 Markdown 块分段并支持上下文展开。Bookmark 只由显式写保存，与已读/ACK/telemetry 分离。三类 cursor 与 Bookmark 尚待实现验收。

OnlineIssuer 规则化自动签发还须覆盖持钥基础证书、明确 owner 子范围委托及同权/收缩续签；超出有效 authority_source、治理权或 CA 资格保持 pending 交上级审核。审计须含 actor、subject、issuer、authority_source、request/csr、policy/version、automatic=true 与 grant 摘要；不得用主观评分自动授权。独立 Test Root selftest 须覆盖这些正反例。

最新复核：已完整重读唯一 ChatGPT 文件夹文档的 01–15 章（`2026-09-26T22:19:27.354Z`）。工作区已有读取长短别名、GraphQL 分离和自动签发审计；post/topic/repo 标签与 `/_search=/_s`、`/_index/by-tag=/_i/by-tag` 已实现并定向测试。todo、其他稳定索引、完整 ReadQuery、三类 cursor、Bookmark 未实现。独立临时数据库 selftest 不能替代最新 `/_test/<run_id>/` 三级 CA/OnlineIssuer 全矩阵。下一轮按 [风险与依赖路线](ITERATION_PLAN.md) 推进。
