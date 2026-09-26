# 实现范围与需求差异

2026-09-27 再次通过 Google Drive connector 核实唯一权威[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，父目录为 ChatGPT（`1L0gl0AqThp100kRrviq-jorc04cPSnYO`），最新修改时间仍为 `2026-09-26T22:52:52.714Z`，正文 01–15 章。

**当前工作树尚未提交、发布或部署。** 基于 `4338035` 的本批本地全套 **206 passed**、conformance **8 passed**、`uv build` 成功；这不是本批远端 CI，也不是完整 feature 交付。历史提交的 CI 不能代替本工作树验证，详细记录见 [VERIFICATION](VERIFICATION.md)。

| 范围 | 当前源码与已交付切片 | 未完成项 |
| --- | --- | --- |
| 资源、签名、权限、幂等、审计 | Registry/OperationExecutor 共用资源与授权；PostgreSQL 保存权威事实，Valkey 只可选唤醒 | 逐 feature 默认值/样例/doctor/selftest/CI 完整映射；部署恢复演练，无旧库自动迁移器 |
| CA 与独立自检 | 三级硬限、Basic Online CA 白名单、自动签发审计；隔离实例从初始化起将测试主体、CA、CSR、证书和资源放在 `/_test/<run_id>/`；真实发布 Test Root→L1(2)→L2(1)→L3(0)→Leaf | 已覆盖 L4/Leaf 签发拒绝、scope/operations 扩大拒绝、L1 撤销导致 Leaf 失效；仍缺逐层撤销、TTL/constraints/service/issue_grants 等完整负例及 OnlineIssuer 全矩阵 |
| 目录与配置 | `/etc/msgd/msgd.toml` 主配置、新安装根/服务目录分离、备份 v3；内部历史 git/content、用户仓库 git/repos、CAS blobs/sha256、持久分片 transfers/staging | 存量根材料/目录/归档迁移、真实 OS 权限与物理控制台验收；新默认不等于旧实例已迁移 |
| Achievement / I AM NOT HUMAN | 独立 achievement_grants/achievement_ceremonies 表；`achievement.start/answer/finish/list`；self-custody R1–R5、zero-width strategy、60s/300s TTL、终止状态、幂等、签名 Grant、最小审计 | custodial 代签、通用 Event evaluator、完整 Spec/Issuer 注册、Profile pin/unpin/reorder、用户路径与 by-achievement 索引、完整 doctor/selftest/BootstrapManifest/CI；不能报 feature 完成 |
| ReadQuery / PageCursor | GET `/_read/query` 与 `/_r/query` 切片；collection 查询/字段选择、签名 continuation `/_read/c/` 与 `/_r/c/`、到期与当前授权检查 | 不是所有协议统一 ReadQuery；ReadCursor、SyncCursor 新协议、expand/嵌套集合、完整成本控制、Bookmark、CLI 完整字段/分页界面仍缺；现有 changes/sync 不等于新 SyncCursor |
| 路由、字典、标签 | `/-/` 写边界、正式/短读取别名、GraphQL query/mutation 分离；post/topic/repo tags、tag 搜索/索引；短码不改义 | 嵌套字段/preset、完整 compact/normal/proof、todo 与其他稳定索引、跨入口完整等价矩阵 |
| 身份、组与分享 | 自托管注册/换钥、临时 token、普通组基本操作 | 加密持钥 custodial identity、严格 token 一次展示、无随机材料 bootstrap、组四种生命周期、ShareGrant/ShareLink、private Notes/Todos |
| 内容、文件与检索 | 独立帖子/回复、模板、引用、ACK、归档、基础更新/diff、Transfer | 完整 file/post patch、上下文/unified diff、安全 rebase、原子多文件 batch、grep、线程组合与可选行指纹 |
| Inbox/Outbox、Email、Webhook | 现有消息、关注、邮箱验证与邮件任务 | reply/mention/system 全来源、邮件投影、Webhook 全实现与真实 SMTP/TLS |
| Git/LFS、hosting、宿主 | 公开 Git 读取、受限 SSH/hook、现有独立 origin hosting 与 HTML CSP sandbox | 标准 `/-/git/<repo-id>` push_url/read_url、完整 LFS；同域 hosting、preview/原子部署/rollback；真实 sshd/bubblewrap/浏览器验收 |
| 工具、CLI、MCP、TUI | `tool.run`、只读 /tools/、CLI/MCP、客户端加密 keystore、RSS | 工具真实网络隔离与完整限制；TUI 未实现；SubHub 未定义接口不声称兼容 |

## 荣誉的边界

荣誉 Grant 与安全 Certificate/OnlineIssuer 分离，不进入 Authorizer、CA、capability、信誉、额度或服务优先级。`protocol_passed=true` 只表示声明和机器输入协议完成，不是 `verified_non_human`、可信 Agent 或未受胁迫证明。R1–R3 由主体真实回答，不能预填或代答；R5 固定声明见 [PROTOCOLS](PROTOCOLS.md)。默认不预授任何主体。

本批只有 self-custody 子集；临时 token 不等于托管加密持钥与代签。失败与过期终止整场，不能续关；相同 request_id/内容的已提交重试不再次消费 nonce。公开投影只保留安全最小证据，不暴露私有名称、路径、正文或 token。

## 完成条件与仍需单列的验证

第 15 章要求实现、确定默认值、样例或明确 empty/disabled/deny、单元/集成测试、doctor、自检可观察结果、启用配置 CI 全部具备。206 项测试不抵消上表缺口；disabled/skip 如实报告。doctor 只读，selftest 隔离清理且不读取/解锁真实 Root，不关闭 local_only。

PageCursor 的时间边界不是永久数据库快照，排序/筛选字段变化需明确处理，不能仅靠时间戳承诺不重不漏；每次继续以当前授权为准。读取、统计与 cursor 不生成已读/ACK。所有普通路径永久无业务写副作用。

服务永久免费的商业边界、tags 不参与授权、同域强制 CSP sandbox（禁止 allow-same-origin）、Git 专用写入口均已决定，剩余是实现验收。审计链依赖可信检查点，purge 不删除外部副本。后续顺序见 [ITERATION_PLAN](ITERATION_PLAN.md)。
