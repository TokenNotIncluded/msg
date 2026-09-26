# 实现范围与需求差异

2026-09-27 对照[权威需求文档](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)核对当前源码。需求基线是 ChatGPT 文件夹中的[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，本轮通过 Google Drive connector 实时核对其修改时间为 `2026-09-26T21:46:43.426Z`、正文为 01–16 章。PostgreSQL + Valkey 是用户后续明确决定，覆盖文档的 SQLite 选型；其余需求继续有效。`provenance.json` 是旧重写来源记录，不能证明已覆盖最新需求；仓库没有该记录所指的完整设计快照。

**当前是已有核心实现、正在补齐新需求的版本，不是完整需求交付，也未部署线上。** 下表“源码已有”只说明存在实现；本地通过范围见 [VERIFICATION](VERIFICATION.md)，真实宿主与完整功能验收仍须分别完成。

本轮路由清理、工具契约更名、分片与字典入口对齐后，Python 3.15 本地全套为 **165 passed**（89.20s），conformance 为 **8 passed**（31.47s），`uv build` 再次成功。此前定向检查已包含其中，不额外累加。这是本地结果，不代表本轮远端 CI 或逐 feature 完成。阶段 1 仍有明确缺口，下表只列已实现或实际通过的范围。验收重点是一次原子操作在各入口的资源、授权、幂等与错误一致，以及 /-/ 外无副作用，不以增加接口数量代替能力交付。

| 范围 | 当前源码与边界 | 下一步 |
| --- | --- | --- |
| 资源、修订、关系、签名、证书、权限、幂等、审计 | core/、security/ 与插件已有实现；注册表统一操作 | 补齐新能力的同一授权契约和逐 feature 验收 |
| PostgreSQL、Valkey | PostgreSQL 保存权威元数据与持久任务；Valkey 可选唤醒；本地测试及提交 `c417b9e` 的远端 CI 通过 | 部署备份恢复演练、旧数据迁移方案；无旧库自动迁移器 |
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
| Git、LFS | 原生公开 Git 读取、受限 SSH push 与 hook 存在 | 普通仓库及 LFS 路径、全部子路径禁止 push/上传/管理 handler，永久只读已确定，写入只能直接走 `/-/` 注册操作；无完整 LFS，标准客户端发现写地址及兼容性待真实验收 |
| 网页托管 | hosting.create/deploy/activate 与独立 origin 实现存在 | 与“同域、不新增子域名”要求冲突；缺 web.preview 与完整 patch→preview→deploy→rollback 验收 |
| 工具、SSH、RSS、密钥库 | 有受控工具 worker、受限命令与 Git hook、RSS、客户端加密密钥库 | 第 13 章规范调用名为 `tool.run`，已完成公开契约更名，`tool.invoke` 仅保留不可执行 tombstone；完整 GET 标量工具参数仍须验收；`/tools/` 及子路径只读发现，参数或方法也不能触发执行。ToolSpec 的名称/说明、版本/摘要、输入输出/并发上限及大输入输出 Transfer 引用须逐项验收。真实 bubblewrap、sshd 与公网/重定向验收未做；SubHub 未定义的接口不声称兼容 |
| TUI | CLI 存在，未见 msg tui 命令 | 待公共契约稳定后实现；不直连数据库，不新增 /login |
| 初始化、doctor/selftest | 默认资源、隔离诊断与恢复已有实现 | BootstrapManifest 缺逐 feature 的默认值、sample、doctor_check、selftest_case 完整映射；不能宣称全部功能验收 |

## 完成条件

最新第 15 章按 feature 验收：实现、确定默认值、样例或明确 empty/disabled/deny、单元/集成测试、doctor、selftest 可观察结果、CI 缺一不可。BootstrapManifest 与测试须共用 feature_id、enabled_by_default、default_config、sample_resource、doctor_check、selftest_case。通过某批测试不能替代整项完成；插件 disabled 应明确 reported disabled/skip，不能伪报 pass。doctor 不改生产状态，selftest 在隔离范围执行并清理，测试 root 也不能关闭 local_only。

用户覆盖 SQLite 的存储选型不取消存储契约验收：实际 PostgreSQL 后端仍须验证事务、并发、恢复与内容引用一致性，不能据选型变更免除要求。当前没有以本批路由或工具更名宣布这些 feature 完成。

## 保留的产品决定与边界

云文档第 16 章仍待消歧：Todo.tags/搜索 tags、Store/订单等商业字段、Git/LFS 客户端如何发现 `/-/` 写地址及兼容性、同域托管隔离、表示后缀与浏览/下载计数。默认保持免费、无个人配额、无全站标签平台、无钱包或信誉风控；不要把未决定的字段补成收费功能。读取不伪造 ACK，也不改变业务状态。

根私钥仅本机管理，当前内部测试不等于真实物理控制台验收。recover 用于可信根材料恢复，rotation --resume 用于轮换日志恢复；任意初始化损坏自动补全并非已验证能力。审计摘要链依赖可信检查点，不能阻止完整写盘权限者重算历史；purge 不抹去已导出的副本。

实施顺序、依赖和可验收出口见 [ITERATION_PLAN](ITERATION_PLAN.md)。
