# 迭代路线与验收出口

依据：2026-09-27 通过 Google Drive connector 读取的[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，修改时间为 `2026-09-26T22:19:27.354Z`，正文 01–15 章。需求用于定义目标，源码与实际测试用于说明进度。最新版已明确 PostgreSQL 长期主数据库，Valkey 保留用户指定的可选唤醒用途；本文件不声称已修改云端需求，也不把路线当成已实现功能。

按下面顺序交付可独立审查的变更。每项先补失败测试，再实现；功能出口共同要求确定默认值、样例或明确 empty/disabled/deny、测试、doctor、selftest 与启用配置的 CI。发布与部署是各自独立步骤。

## 主旨与本批边界

每次迭代只新增可组合的原子能力：Resource/Revision 保存事实，统一 Authorizer 决定访问，OperationExecutor 负责提交。协议、CLI、MCP 与后续 TUI 只适配同一契约；不建设通用工作流引擎，不用新的展示层代替缺失能力。简单操作一次提交即给必要回执，不额外拆 prepare/execute/readback；正文、完整证书和线程关系按需获取。

本组修改后，Python 3.15 全套 165 passed（89.20s），conformance 8 passed（31.47s），uv build 再次成功；具体命令与耗时见 [VERIFICATION](VERIFICATION.md)。POST、签名 GET 与 MCP 已迁至 /-/，旧写入口拒绝；字典提供最小索引、分级详情和保护同码语义的首发快照。新建 post/reply 使用 .md；新安装包含 /AGENTS.md，不再 seed /rules。本组已补真实 /.agents/skills/msg-entry/SKILL.md 与 /tools/，以及授权后只读 308；本轮再补 GET-only token/bootstrap 标量写、expected 前置条件和 4096 字节字段上限（受默认 8192 字节原始路径限制）。这些变更已纳入最终全套，定向检查不重复累加。

阶段 1 尚未完成：GET-only token/bootstrap 标量写已通过本地全套和远端 CI，但严格一次 token 展示、无随机材料 bootstrap 与复杂嵌套输入仍缺；旧式无后缀 URL 对已有 .md Post 已实现授权后只读跳转，私有内容不泄露目标；旧数据库真正无后缀 Post 的自动改名/别名迁移仍未实现。/tools/ 与入口技能已通过本组全套，完整技能集与局部规则发现仍待补齐。顶层 enum/const 已编码，嵌套字段/preset、compact/normal/proof 完整投影与持续 token/往返测量仍待补齐。发布与宿主验收另行进行。后续阶段仍按权限与数据依赖推进。

## 优先修复：CA 深度、根隔离与自检

先补 Root→L1→L2→L3→Leaf 的硬上限与 2/1/0 子深度；三级硬限代码已加入，当前仍处验证阶段。层级只能从父链推导，不授予权限；签发/发布/验证均查逐项收缩。Basic Online L1 固定深度 0 与最小白名单，拒绝特殊/ca_only 能力和下级 CA。

同步迁移根状态至 `/var/lib/msgd-root/`，公开信任材料放 `/etc/msgd/trust/`；持久、缓存、运行目录分别 `/var/lib/msgd/`、`/var/cache/msgd/`、`/run/msgd/`，上传 staging 必须跨重启保留。须有存量根材料和引用迁移验证，不能静默换根。doctor 只读查完整 CA 边界；selftest 在 `/_test/<run_id>/` 用独立 Test Root 构造三级链，覆盖越权、超深、撤销、Leaf 无签发权，绝不读取/解锁真实 Root。

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

按已确定的 `/-/git/<repo-id>` push URL 补同域 Smart HTTP/LFS 写入口，并返回 read_url/push_url，Files/LFS/transfer 共用 BlobStore。托管必须采用同域 HTML CSP sandbox，脚本最多 allow-scripts、禁止 allow-same-origin；补 web.preview、原子部署与回滚；当前独立 origin 实现不能直接宣称符合新需求。

验收：真实 git/LFS 客户端 clone/fetch/push/upload/download，公开读和受限写同时成立，所有写传输位于 /-/，服务端拒绝明确二进制进入普通 Git。浏览器验证同域托管隔离方案与 Content-Type；可打开 /@root/web/index.html，常规隔离主体完成 patch→preview→deploy→readback→rollback。root 发布只走本机。真实 sshd 禁 shell/转发、bubblewrap、公网/私网/重定向以及物理控制台流程均有宿主证据。

## 6. TUI 与发布收尾

公共契约稳定后实现 msg tui：Home、Inbox/Outbox、Topics/Following、线程、Notes/Todos、Files、Groups、搜索和凭据状态。禁止 Store 订单/余额/付费能力入口；不引入 /login、浏览器 Cookie 或数据库直连。

验收：无凭据有明确身份入口，分页与局部读取，按权限显示，pending/failed/uncertain 如实呈现；终端操作走与 CLI 相同的公共契约。BootstrapManifest 按 feature_id 补齐默认配置、样例、doctor/selftest 映射，功能缺项不能报完成。干净安装与 CI 通过后，再进行独立部署、线上读取与写入回执核对、备份恢复和回退演练。

## 已定的新能力，不能继续作为待决定项

tags：Registry 声明 taggable，post/topic/todo/repo 默认支持规范化可选 tags，search tag 与 `/_index/by-tag/<tag>` 索引逐条授权，tags 不影响安全父链或权限。

读取：canonical path 配 Accept/fields；GET-only 使用 `/_r/<resource_id>/json|meta|raw|history|rev/<revision_id>`，改名移动后稳定、全部先授权。浏览/下载量是异步 telemetry，允许近似重复，不能改资源/修订/generation/已读/ACK，更不能参与授权或计费。

商业字段：永久免费的基础能力与禁止余额、充值、订单、付费会员/能力已经确定。Git push URL 和同域 sandbox 也已确定；剩余是实现与真实客户端/浏览器验收，不能再以待产品决定阻塞。

本路线的新条目不在此前 165 项测试证明范围内；实现后按第15章逐 feature 补默认值、样例、doctor/selftest 与 CI。

## 22:19 修订的下一步

当前开发批次已加入新安装数据布局、备份 v3、CA 三级硬限和 `/_r/` 稳定 ID 投影；Basic Online CA 白名单、自动签发审计以及正式读取别名/GraphQL 分离已有本批代码，完整自检与最终回归仍在推进。最终全套尚未完成，此前提交的测试/CI 数字仅为历史证据，不证明本批完成。新安装默认值不等于存量根材料、目录或归档已自动迁移。

最新读取目标是 `/_read/`，`/_r/` 是永久短别名；`/_search=/_s`、`/_index=/_i` 同样要求直接命中同一 handler，内容、授权、缓存、错误和 cursor 完全等价且不重定向。只读 GraphQL 为 `/_read/graphql`（短别名 `/_r/graphql`）且仅 query；`/-/graphql` 仅 mutation。结构化读取统一 ReadQuery，并限制深度、节点数、响应大小、查询成本、集合页大小和超时。这些新增目标尚未完整实现，已有 `/_r/` 定向验证不能替代验收。

PageCursor 与 ReadCursor 使用 `/_read/c/<opaque_cursor>`，SyncCursor 使用 `/_read/s/<opaque_cursor>`，均有 `/_r/` 短形式。服务器直接返回 continuation，cursor 签名/MAC、有限期且每次重新授权；PageCursor 固定查询和 snapshot，ReadCursor 固定 Revision、按 Markdown 块分段并支持上下文展开。Bookmark 只由显式写保存，与已读/ACK/telemetry 分离。三类 cursor 与 Bookmark 尚待实现验收。

先完成 CA/目录当前批次回归，再做正式读取别名、GraphQL query/mutation 分离和 ReadQuery；之后逐步完成三类 cursor、Bookmark、搜索/索引与 tags。OnlineIssuer 的来源重验和 automatic 审计随 CA 验收补齐，拒绝扩权或保持 pending，不能自动降级绕过审核。

## 重新核实后的风险与依赖顺序

再次从唯一权威来源——Google Drive `ChatGPT` 文件夹（`1L0gl0AqThp100kRrviq-jorc04cPSnYO`）中的《msg.lmm.best｜项目设计》（`1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0`）完整读取 01–15 章，修订时间仍为 `2026-09-26T22:19:27.354Z`。下列路线区分工作区代码与完成验收，不把本地文档视为第二个需求源。

1. 先收敛当前安全与持久化批次：三级 CA、Basic Online 最小白名单、authority_source 当前状态重验和自动审计，根/服务密钥隔离、新安装目录及备份恢复。补齐 `/_test/<run_id>/` 独立 Test Root 的完整逐级签发/收缩/撤销矩阵与 doctor 检查；之后跑最终全套。现有 selftest 是隔离临时数据库流程，不能据此声称已覆盖新版完整矩阵。
2. 完成统一只读查询底座：正式/短读取别名与 GraphQL 分离已有工作区代码；下一步是 ReadQuery、逐对象授权、字段裁剪、成本限制。随后实现 PageCursor/ReadCursor/SyncCursor 的 MAC、固定 Revision/snapshot、直接 continuation、撤权与过期行为，最后再加显式 Bookmark。
3. 在统一授权和分页上实现 SearchQuery/IndexQuery、`/_search=/_s`、`/_index=/_i`、taggable 元数据与 tag 索引；索引可重建但撤权必须立即生效。该组仍未完整实现。并行补严格 token 一次交付、托管身份、组生命周期与 ShareGrant，作为 Notes/Todos 和隐私投递的前置条件。
4. 按依赖补 file/post patch、grep、线程、分享与个人空间，再补 Inbox/Outbox 全来源、Webhook/邮件及 TUI。当前已有基础操作不能替代这些完整能力。
5. 单列原生 Git/LFS `/-/git/<repo-id>` 写入口与 read_url/push_url、同域托管 CSP sandbox/preview/deploy/rollback，以及真实 sshd/bubblewrap/SMTP/浏览器验证；不能以普通 Git 只读通过代替它们。完成逐 feature 默认值/样例/doctor/selftest/CI 后再讨论发布和部署。
