# 架构与提交边界

本文说明当前底座与必须保持的边界，不表示最新云盘需求已全部实现。需求差异见 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)，实施顺序见 [ITERATION_PLAN](ITERATION_PLAN.md)。需求基线是 ChatGPT 文件夹中的[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，本轮通过 Google Drive connector 实时核对其修改时间为 `2026-09-26T21:46:43.426Z`、正文为 01–16 章。PostgreSQL + Valkey 是用户后续明确决定，覆盖文档的 SQLite 选型；其余需求继续有效。

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

HTTP 协议操作只从 `/-/` 分流。旧 `/!`、`/~`、`/run/j|gz` 与 `/mcp` 不再有兼容 handler；普通资源、RSS、latest 与原生 Git 的公开读取在执行前核对注册操作为 read。原生 Git 的 POST `git-upload-pack` 只读，`git-receive-pack` 不开放。普通 Git/LFS 路径及子路径永久只读，写入只能直接走 `/-/` 注册操作；标准客户端发现写地址的兼容性仍待验证，不允许普通路径代理、重定向写入或额外子域名。旧兼容 handler 删除已纳入最终回归，本轮 Python 3.15 最终全套 165 passed、conformance 8 passed、uv build 成功。新 .md 规范路径、/AGENTS.md、/tools/ 已覆盖新安装；旧库存量链接和完整输出投影仍需单独迁移与验收。

临时 token 不能代替托管身份；mode/证书不能代替可追踪 ShareGrant；现有全文更新不能代替 patch/grep；现有 hosting 独立 origin 不能满足同域托管要求。新增这些能力应继续复用资源、授权、事务与事件，不另建业务后端。

第 13 章规定工具调用名为 `tool.run`；`/tools/` 只发现凭据允许的工具，路径本身绝不执行。公开契约已迁移为 `tool.run`，`tool.invoke` 仅保留不可执行 tombstone，短码不改义复用。`/-/transfer` 已提供六种分片操作的完整 OperationRequest POST 与只读 GET 发现；两段式 `/-/d/<namespace>/<operation>` 已实现。上述本地验证不代表完整工具/Git LFS/托管身份、逐 feature 验收或本轮远端 CI。

最新第 15 章按 feature 要求实现、默认值、样例或明确空/禁用/拒绝状态、测试、doctor、selftest 与 CI 全部具备。插件 disabled 必须如实报告 disabled/skip；doctor 只读，selftest 隔离清理，root 测试也不关闭 local_only。第 11 章还要求 Files/LFS/Transfer 共用 BlobStore、下载流式或范围读取，禁止无鉴权 CAS 直链和把明确二进制直接写入普通 Git 对象；这些均须单独验收。
