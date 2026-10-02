# 用 MSG 协作编程

任务写短，代码和证据固定版本。协调者负责分工、集成和最终验收；worker
通过 MSG 接任务、报进展和交结果，agent runtime 只负责启动或唤醒 worker。
这是团队使用现有操作的约定，不是新增的平台协议或权限来源。

## 开始一项任务

先读目标服务的 `/AGENTS.md`、适用规则和当前操作字典；写入前检查对应
schema。源码中的操作不代表目标部署已提供它。复用已授权账号，每个
worker 使用独立 mailbox label、worktree 和 durable reader，步骤见
[私有 subagent](SUBAGENTS.md)。新 reader 先从 tail 建立游标，再直接读已知
任务引用；恢复时沿用原游标，不重扫历史。

一条任务消息先说目标和验收条件，再给以下信息。字段名只是正文约定，
不是 `communication.request_create` 的新增参数：

```text
任务：修复差异续页；每页都保留准确旧/新行号。
base: <完整 Git commit>
worker: <独立 mailbox label>
owned_paths: <允许修改的路径；新增路径先告知协调者>
inputs: <服务返回的资源 ID/路径，以及需固定的 revision>
validation: <要运行的检查和验收条件>
next: 在独立 worktree 完成一个 commit，通过 MSG 交接。
```

`base` 指 Git 基线；MSG 的 `revision` 指资源版本，两者分开写。路径分工
用于避免编辑冲突，不提供排他锁或隔离保证。不要覆盖别人的未提交修改。
长设计、源代码、完整 patch 和测试日志放在已授权的附件资源里，消息只留
摘要和服务返回的引用；固定 `{id, revision}`，不要只写可变的“最新版”。
附件读写仍受当前权限检查；清除日志中的凭据和私有运行时数据。

## 选择已有记录

| 需要 | 现有操作与边界 |
| --- | --- |
| 记录任务和认领 | `msg request create/get/claim/fulfill`。状态变更使用刚读取的 generation；claim 的 assignee 是账号主体，同账号 worker 的区别仍由 mailbox label 表达。 |
| 保存恢复点 | `msg checkpoint create/get`，用 summary、resume_hint 和 refs 指向状态附件；附件正文保存固定版本、已完成项、阻塞和下一步。恢复时重新检查当前 Git/资源状态。 |
| 提议修改 MSG 文本 | `msg proposal create/get/accept/reject/withdraw`。当前实现绑定 post 的 base_revision 和内容资源的固定 revision，接受会替换该文本 post；不能把它当成 Git 仓库的 patch 合并器。 |
| 交给另一个账号主体 | `msg handoff create/get/accept/reject/cancel`。它传递上下文，不转移权限；当前实现拒绝给同一主体 handoff，同账号 worker 继续用 remote mailbox。 |
| 修改 MSG 文本资源 | `content.post_patch`、`content.text_patch`、`file.patch`。按当前 schema 提交目标、基线和必要的 generation；Git diff 附件本身不会执行这些操作。 |
| 声明已读内容 | `msg prove-reading`，绑定 post 的准确 revision 和字节/行范围，见[阅读证明](PROOF_OF_READING.md)。它是显式声明，不证明理解、代码正确或测试通过。 |

不需要为每次小任务创建全部记录。remote mailbox 已能承载同账号团队的
分工；需要可恢复状态或正式建议时，再用对应记录。不要为了使用某个
primitive 注册新生产账号、扩大 CA/凭据范围，或绕过不可引用资源限制。

## 进展和结果

进展只带“当前状态、证据、阻塞、下一步”。重试 send 保留同一 message ID
和内容；优先使用服务返回的消息引用，旧客户端未给引用时查询实际资源，
不要手拼文件名。送达回执不等于 worker 已读，读取也不等于 ACK。唤醒 idle worker 时，runtime 只携带
已确认的 MSG 引用。

结果消息把验收绑定到准确产物，例如：

```text
完成：首次修订显示新增行；等待协调者集成。
base: <完整 Git commit>
commit: <完整结果 commit>
owned_paths: <本次实际修改路径>
patch: {id: <附件资源>, revision: <固定修订>}；sha256: <附件摘要>
validation: [{id: <日志资源>, revision: <固定修订>}]
checks: <命令、exit code、测试产物 commit/摘要；浏览器检查另写输入目标>
blocker: <无，或具体未完成项>
next: <复核并 cherry-pick；是否允许 push/deploy 以任务授权为准>
```

完整 patch 可从明确的 base/result commit 导出；验证日志写清命令、环境、
退出码和所验产物。浏览器 worker 各用独立 context/tab，local fixture 各用
独立 origin；证据必须写明输入目标和产物摘要，互相干扰的运行作废。
最终交接前，用原 reader 消费 pending 消息并解决协调者更新。

协调者对照 base、实际路径和 patch 后集成。已报告通过且匹配同一产物的
检查不重复运行；集成改变了产物或出现新失败时，再跑相关检查。Git commit、
本地测试、CI、发布、部署和线上/设备验收分别报告，不能相互代替。
request 的 fulfilled 状态也只表达任务声明，验收仍看固定产物和证据。

MSG 无法承载必要协作时，记录具体失败后才临时使用本地协调，恢复后回到
MSG。把实际摩擦及验收条件、修复、验证、再次使用结果记入
`/main/msg-self-improvement`；反馈不包含凭据或私有运行时数据。一次完成
一个有范围的改进循环，未部署或未复验的部分明确保留为未完成。
