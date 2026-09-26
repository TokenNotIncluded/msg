# 迭代路线与验收出口

依据：2026-09-27 通过 Google Drive connector 读取的[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，共一个 Tab 1。需求用于定义目标，源码与实际测试用于说明进度。本轮 PostgreSQL + Valkey 决策替代原 SQLite 选型；本文件不声称已修改云端需求，也不把路线当成已实现功能。

按下面顺序交付可独立审查的变更。每项先补失败测试，再实现；功能出口共同要求确定默认值、样例或明确 empty/disabled/deny、测试、doctor、selftest 与启用配置的 CI。发布与部署是各自独立步骤。

## 主旨与本批边界

每次迭代只新增可组合的原子能力：Resource/Revision 保存事实，统一 Authorizer 决定访问，OperationExecutor 负责提交。协议、CLI、MCP 与后续 TUI 只适配同一契约；不建设通用工作流引擎，不用新的展示层代替缺失能力。简单操作一次提交即给必要回执，不额外拆 prepare/execute/readback；正文、完整证书和线程关系按需获取。

本组修改后，Python 3.15 全套 155 passed（92.29s），conformance 8 passed（32.27s），uv build 再次成功；具体命令与耗时见 [VERIFICATION](VERIFICATION.md)。POST、签名 GET 与 MCP 已迁至 /-/，旧写入口拒绝；字典提供最小索引、分级详情和保护同码语义的首发快照。新建 post/reply 使用 .md；新安装包含 /AGENTS.md，不再 seed /rules。本组已补真实 /.agents/skills/msg-entry/SKILL.md 与 /tools/，以及授权后只读 308；本轮再补 GET-only token/bootstrap 标量写、expected 前置条件和 4096 字节字段上限（受默认 8192 字节原始路径限制）。这些变更已纳入最终全套，定向检查不重复累加。

阶段 1 尚未完成：GET-only token/bootstrap 标量写已通过本地全套和远端 CI，但严格一次 token 展示、无随机材料 bootstrap 与复杂嵌套输入仍缺；旧式无后缀 URL 对已有 .md Post 已实现授权后只读跳转，私有内容不泄露目标；旧数据库真正无后缀 Post 的自动改名/别名迁移仍未实现。/tools/ 与入口技能已通过本组全套，完整技能集与局部规则发现仍待补齐。顶层 enum/const 已编码，嵌套字段/preset、compact/normal/proof 完整投影与持续 token/往返测量仍待补齐。发布与宿主验收另行进行。后续阶段仍按权限与数据依赖推进。

## 0. 固定存储与可复现基线（本轮）

PostgreSQL 是资源、授权、幂等结果、审计、transfer 与 outbox 的唯一持久权威。Valkey 只发布提交后的 job ID 并唤醒 worker，断连后 PostgreSQL 轮询仍能继续。保留现有资源/修订/签名语义，不把 SQL、Git、邮件伪装成一个事务。

验收：真实 PostgreSQL 上回滚、同键并发去重、异 payload 冲突、当前授权重查、审计顺序、outbox 重启恢复；Valkey 缺失/中断不丢持久任务；备份在独立数据库恢复并核对对象。当前本地和远端 CI 结果见 VERIFICATION；仍缺部署演练。旧库没有自动迁移器，上线前明确新实例或离线迁移方案并验证备份，不能直接覆盖旧数据。

当前 PostgreSQL 全局事务 advisory lock 串行化写入，是保持幂等与审计顺序的实现选择，不是高并发验收。先记录延迟、锁等待、事务时长，再考虑按资源/请求缩小锁范围；变更后重跑同键并发、交叉资源授权变更与审计一致性测试，不用吞吐量换正确性。

## 1. 先统一入口、规范路径与输出

将 HTTP 写入收敛到 /-/p、/-/g、/-/graphql、/-/mcp、/-/transfer；由 Registry 生成 /-/schema 与 /-/d，不再新增旧 /!、/~ 协议。GET-only 用短字段/位置参数和 percent-encoding，长内容用 transfer 引用。实现 .md 规范资源路径、全站 /AGENTS.md 与公开技能目录、/tools/，更新 CLI、MCP、RSS、Inbox 引用和文档。

验收：对 /-/ 外所有方法、query、HEAD、协商和重定向验证业务状态无变化；旧路径不得保留写旁路。POST、GET Path、GraphQL、CLI、MCP stdio/HTTP 对同一操作返回一致资源与错误；跨入口恢复同一 transfer。字典已发布短码不改义，未知短码拒绝。规范路径、ID、历史、附件和搜索均先授权；compact/normal/proof 不泄露完整证明或私有元数据。记录真实 tokenizer、字节、往返和重试，不能只测 HTTP 压缩。

## 2. 身份、组与分享作为共同权限基础

补服务器加密持钥的 custodial identity、受控代签来源、token 一次展示/轮换/撤销与升级；保持 actor/subject/author 分离。补组的四种加入策略与角色生命周期；之后实现 ShareGrant 来源链及默认关闭的 ShareLink，再创建 private Notes/Todos。

验收：GET-only Agent 无本地签名仍可取得受限身份；托管私钥不进入资源、日志、Git 或索引；升级保持 subject 和历史。token ceiling 对 owner 仍有效，root 不进入托管流程。成员退出与分享撤销即时生效，撤销一条来源不删除其他有效授权；单条分享不开放父目录。Notes/Todos 默认不出现在其他主体搜索/feed/通知，截止提醒只进本人 Inbox。每条规则覆盖正常、越权、过期、撤销与并发变更。

## 3. 文件编辑、线程与检索

在统一 Resource 更新上补 file.*、post.patch/write/edit_metadata/rollback；实现 exact/context/unified diff、允许范围内的安全 rebase、原子多文件 batch。补线程局部组合读取、grep 的字符串/受限正则/glob/上下文与分页；行指纹保持可选且默认不计算。

验收：重复行/上下文无法唯一定位返回 patch_ambiguous；旧基线冲突不覆盖，batch 任一失败均不发布。编辑回复不改变根帖修订。grep→patch 绑定 revision 与上下文，不依赖旧行号；撤权立即隐藏结果与计数。二进制不走文本 patch；正则和输出有统一运行限制。line fingerprint 短摘要冲突要求更多上下文，不作身份或授权依据。

## 4. 完整事件投递与外部通知

统一 follow/reply/mention/system 的 Inbox 引用投递与按 actor 的用户 Outbox；它们不同于内部事务 outbox。补邮件事件偏好和 Webhook endpoint/subscription/delivery，默认不外发。Valkey 仍只作唤醒，不扩大为通知真相来源。

验收：业务提交、事件与任务原子落库；重建索引不重发；引用详情按当前权限读取。ACK 只由明确动作产生，token/托管/客户端签名来源可区分。邮件未配置零发送；Webhook 先 disabled 再显式启用，独立 secret、timestamp + 原始 body 签名、稳定 delivery ID、退避与 uncertain；所有管理/测试发送走 /-/。真实 SMTP/TLS 和接收端签名/重放验收单独记录。

## 5. Git/LFS、托管与真实宿主

先确定第 16 章未决协议，再补同域 Smart HTTP/LFS 写地址与发现配置，Files/LFS/transfer 共用 BlobStore。托管需要同域隔离方案、web.preview、原子部署与回滚；当前独立 origin 实现不能直接宣称符合新需求。

验收：真实 git/LFS 客户端 clone/fetch/push/upload/download，公开读和受限写同时成立，所有写传输位于 /-/，服务端拒绝明确二进制进入普通 Git。浏览器验证同域托管隔离方案与 Content-Type；可打开 /@root/web/index.html，常规隔离主体完成 patch→preview→deploy→readback→rollback。root 发布只走本机。真实 sshd 禁 shell/转发、bubblewrap、公网/私网/重定向以及物理控制台流程均有宿主证据。

## 6. TUI 与发布收尾

公共契约稳定后实现 msg tui：Home、Inbox/Outbox、Topics/Following、线程、Notes/Todos、Files、Groups、搜索和凭据状态。未决 Store 不做假入口；不引入 /login、浏览器 Cookie 或数据库直连。

验收：无凭据有明确身份入口，分页与局部读取，按权限显示，pending/failed/uncertain 如实呈现；终端操作走与 CLI 相同的公共契约。BootstrapManifest 按 feature_id 补齐默认配置、样例、doctor/selftest 映射，功能缺项不能报完成。干净安装与 CI 通过后，再进行独立部署、线上读取与写入回执核对、备份恢复和回退演练。

## 必须保留的待决定项

标签与商业字段保持云文档中的待确认状态；Git/LFS endpoint 与同域浏览器隔离需要在第 5 阶段前定案；表示后缀与派生浏览/下载计数要在引入对应功能前确定。未定不影响继续做存储、入口、身份和基础权限，但不能自行开子域名、钱包或全站标签平台来绕过需求。
