# 本地与 CI 验收记录

## 2026-09-27 托管升级与同域托管批次

权威 Google Drive `ChatGPT` 文件夹设计仍为 `2026-09-27T00:35:34.164Z` 修订。当前未提交工作树本地 Python 3.15/PostgreSQL 下 `uv run python -m pytest -q` 为 **295 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 与 `git diff --check` 通过。托管升级已测试新 Ed25519/age-X25519 双钥持有证明、本地待提交 journal、已知 age 密文存在时的 `pending_rewrap`、空已知库存时同事务新钥绑定/旧 token 撤销/vault 销毁/审计，以及最终响应丢失后用新签名钥查询结果；外部密文迁移仅记录本人声明，逐对象 rewrap 尚无通用流程。托管静态文件改由主域匿名只读服务，HTML/HEAD/304/206/错误响应强制 CSP sandbox，本批完全禁用脚本；危险格式强制下载。

真实浏览器使用本机 DNS 直接访问 `http://light.local:18144/@browser-probe/web/index.html`，不是 `--resolve`：页面快照仍为 “script not run”，浏览器控制台明确提示 sandbox 未允许脚本，网络记录仅 HTML GET，产品托管页**没有发出**私有 API 请求。另起受控探针页 `http://light.local:18145/` 并设 `sandbox allow-scripts`，其不属于产品托管响应；它从 opaque `Origin:null` 对 `http://light.local:18144/_read/t_private/json` **实际发出** GET，服务端访问日志为 403，浏览器报 CORS 无允许来源，脚本不能读取响应。此前 data: 源探针因浏览器 Private Network Access 在发出前拦截，不用作服务端拒绝证据。测试服务按预定时间退出，浏览器会话、临时数据库/目录和端口已清理。当前结果不证明 JS 托管、候选部署 preview、所有重定向/Service Worker/凭据组合或完整同域安全隔离。本批尚无远端 CI/线上部署结果。

## 2026-09-27 托管身份、SyncCursor 与显式 rewrap 批次

权威 Google Drive `ChatGPT` 文件夹文档读取至 `2026-09-27T00:35:34.164Z`。提交 `fcf6ae9` 前，本地 Python 3.15/PostgreSQL 下 `uv run python -m pytest -q` 为 **284 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 与 `git diff --check` 通过。托管创建使用独立 Ed25519/age-X25519 私钥和域分离 AES-GCM vault；受控内容写入的 Revision 由 vault 签名并标明 custodial 来源，未接通代签的 token 写入明确拒绝。`/_read/s`/`/_r/s` 使用独立 SyncCursor，已见引用集合加密后由 MAC 保护，撤权只返回最小 ID，最多跟踪 64 个引用。QueryRef 的私有描述 File 由维护任务在有效期后按引用边界回收，GET 不清理。另有显式选定 age keystore 条目的旧钥→新 recipient rewrap，保留旧 Revision 和旧钥，stale base revision 拒绝。完整托管升级/网络代解密、严格 token 一次展示及丢响应恢复、超过 64 引用的长期同步和权限新增后的旧事件回补尚缺；提交已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36287082964)已通过，尚无线上验证。

## 2026-09-27 QueryRef、发行来源与自托管恢复备份批次

权威 Google Drive `ChatGPT` 文件夹需求读取至 `2026-09-27T00:35:34.164Z`。提交 `65acff3` 前，本地 Python 3.15/PostgreSQL 环境下 `uv run python -m pytest -q` 为 **274 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 与 `git diff --check` 通过。新增复杂 ReadQuery 的 Transfer 分片封存、15 分钟 QueryRef 和短续页，读取逐次重验当前主体/源文件 ACL/摘要；QueryRef 的查询描述目前保留为私有 File，未自动清理。发行规则 Revision 的可选来源字段、history 游标与 `requires_rules[]` 已有回归。恢复切片由本人签名 opt-in Policy、绑定私有 age keystore 确切 Revision 的 Envelope 元数据及客户端标准 age 多 recipient 离线演练组成；两把指定恢复钥分别可解，无关钥不能解，解密不授账号权限。服务端将 recipient 集合标为 `owner_declared_unverified`，不据密文头声称已验证。账号恢复授权、旧密文 rewrap、完整 Custodial 双钥、QueryRef 描述自动回收等仍缺。提交已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36285859198)已通过：Ubuntu/PG16/Valkey，age 1.1.1 安装成功，恢复互操作测试实际执行，274 tests、8 conformance 和构建成功。尚无线上验证。

## 2026-09-27 源码规则、LinkSet 与个人文档批次

本批按 Google Drive `ChatGPT` 文件夹需求读取至 `2026-09-27T00:35:34.164Z`。提交 `8fdfb85` 前，本地 Python 3.15/PostgreSQL 下 `uv run python -m pytest -q` 为 **262 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 成功，`git diff --check` 通过。源码发行的短 `/AGENTS.md`、`/_rules` 索引和八个规则分片已打入 sdist/wheel，启动按文件 digest/version 幂等同步 Revision；`/wiki` 按普通签名资源治理。LinkSet、逐项授权的集合导航和真实 Revision diff，以及主动写入的私有 Notes/SOUL/主体 AGENTS 有定向回归。全套首轮曾因短 AGENTS 未列无凭据 `/-/p/<operation>` 模板失败；把源码规则版本升到 2 后，入口/规则定向与最终全套通过。发行来源字段的完整 Revision/history 投影、requires_rules、HTML/TUI 导航、个人文档独立 manifest 签名与完整秘密识别仍缺。提交已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36284478373)已通过；没有线上部署验证。

## 2026-09-27 Topic、被动 GET 与纯路径读取批次

权威 Google Drive `ChatGPT` 文件夹文档本批读取到 `2026-09-27T00:20:54.350Z`。提交 `befa5ee` 前的本地 Python 3.15/PostgreSQL 验证：`uv run python -m pytest -q` 为 **249 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 成功，`sh -n deploy/prepare-service.sh` 和 `git diff --check` 通过。本批含 TopicMembership/Ban 及真实治理 Event、HTTP 虚拟 `/_events.md`、`/-/g` 被动客户端防误触发、简单 ReadQuery/搜索的纯路径 q/1 与短码快照更新。隔离 selftest 曾因 Topic bootstrap 的测试子树建立顺序出现 `not_found`，调整种子顺序后定向与全套均通过。QueryRef、完整 RouteSpec、Topic 增量投影，以及最新新增的 `/_rules`、`/wiki`、SOUL/主体 AGENTS、LinkSet/SearchQuery 全范围仍缺。提交已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36282759370)已通过；未线上部署。

## 2026-09-27 双钥、主体别名与协议版本批次

权威 Google Drive `ChatGPT` 文件夹文档读取至 `2026-09-26T23:58:21.986Z`。本地 Python 3.15/PostgreSQL 环境下 `uv run python -m pytest -q` 为 **228 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 成功，`sh -n deploy/prepare-service.sh` 与 `git diff --check` 通过。新增 age/X25519 recipient 使用本机 `age-keygen -y` 与 `age` 加解密做互操作；注册/升级 v2 要求独立加密子钥，旧 v1 保留契约并明确拒绝缺钥注册；主体短长路径别名经真实钥、SSH、keystore、Inbox 数据验证。另含 DM、ReadCursor、presence/claim 与默认 1 MiB Git HTTP push 切片。提交 `e01dacc` 已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36281301900)已通过；custodial 双钥、恢复/遗言、Topic 治理、纯路径 QueryRef、新增 passive-client guard 等尚未纳入该提交，不能据此宣称完整需求或线上已更新。

`e01dacc` 另从临时独立 worktree、临时 PostgreSQL 和临时初始化服务，在本机 `10.174.197.165:18143` 监听，以真实 DNS 直接请求 `http://light.local:18143`（没有 `--resolve`）：`/`、`/AGENTS.md`、`/_r/query?root=/main&first=2`、`/_read/graphql` query 均返回 200，普通 `/main` POST 返回 405。服务按预设时间正常退出；worktree、临时数据库和目录已清理，端口无残留监听。这验证本地域名、Host 校验及本机 HTTP 路由，不代表生产部署或浏览器托管隔离。

## 2026-09-27 荣誉、读取游标与隔离 CA 自检批次

权威 Google Drive `ChatGPT` 文件夹需求本批读取到 `2026-09-26T22:52:52.714Z`。本地 Python 3.15/PostgreSQL 环境下 `uv run python -m pytest -q` 为 **206 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 成功；`sh -n deploy/prepare-service.sh`、`git diff --check` 通过。配置文件新安装改为 `msgd.toml`，旧 `server.toml` 单独存在时可读；隔离自检在 `/_test/<run_id>/` 验证 Test Root→L1→L2→L3→Leaf、越层/扩大部分授权和撤销。荣誉 R1–R5 目前仅 self-custody；ReadQuery 仅有 GET collection/PageCursor 切片。完整 CA 负例矩阵、托管代签、ReadCursor/SyncCursor、Profile 与成就索引仍缺。提交 `f085e7f` 已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36278494686)已通过；未线上部署。

按用户提供的本地域名另做真实 HTTP smoke：在临时 PostgreSQL 与临时初始化服务上监听 `127.0.0.1:18142`，以 `curl --resolve light.local:18142:127.0.0.1 --noproxy '*'` 保持 Host 为 `light.local`。`/`、`/AGENTS.md`、`/_read/query?root=/main&first=2` 及等价 `/_r/query` 均为 200；普通 `/main` 的 POST 为 405，旧 `/!foo` 为 404；`/_read/graphql` query 为 200，`/-/graphql` 对 query 返回 `graphql_effect_mismatch`。测试服务已停止、临时库与目录已清理。这是本机隔离实例的 HTTP 验证，不是生产域名或同域托管浏览器隔离验收。

## 2026-09-27 ChatGPT 文档对齐批次

权威需求为 Google Drive `ChatGPT` 文件夹中的《msg.lmm.best｜项目设计》，本轮读取修订时间 `2026-09-26T22:19:27.354Z`，覆盖 01–15 章。当前工作树在 Python 3.15、PostgreSQL 测试环境下 `uv run python -m pytest -q` 为 **197 passed**；新增 CA、标签、只读入口等定向复跑 **29 passed**；修复只读 GraphQL 客户端入口后 `uv run python -m pytest -q conformance` 为 **8 passed**；`sh -n deploy/prepare-service.sh`、`git diff --check` 和 `uv build` 通过。测试通过只证明被覆盖的本地代码行为，不代表第 15 章所有 feature 已完成，也不代表线上部署。完整独立 Test Root CA selftest、ReadQuery/cursor、todo、Git 标准 push URL 与同域托管等仍待实现。

该批已提交为 `4338035`，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36277543483)已通过。后续文档与代码变更属于下一批，不能借用此 CI 结果。

## 2026-09-27 存储迁移与协议入口迭代

本轮本地验证使用 Python 3.15.0rc2、隔离 PostgreSQL 18 与临时 Valkey 9；没有替换生产数据库，也没有发布或线上验收结果。[远端 CI 运行](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36272190647)在提交 `c417b9e` 上通过，使用 PostgreSQL 16 与 Valkey 服务容器。

| 实际命令 | 结果 |
| --- | --- |
| `uv run --no-project --python 3.15 --with-editable . --with pytest --with pytest-asyncio pytest tests -q` | 155 passed in 92.29s，本批修改后全套重跑 |
| `uv run --no-project --python 3.15 --with-editable . --with pytest --with pytest-asyncio --with tiktoken pytest conformance -q` | 8 passed in 32.27s |
| `uv build` | GET-only 标量值上限 4096 字节修改后最新构建成功，生成 0.1.0a1 sdist 与 wheel |

远端 CI 同一提交上记录 **155 passed（164.99s）**、**8 passed（43.59s）**，并成功生成 sdist 与 wheel。第一次运行因 runner 的 `pg_dump` 比 PostgreSQL 18 服务容器旧，以及 RSS 首次分页依赖数据库排序规则而失败；修复提交 `c417b9e` 后全流程通过。
| `python3.15 -m compileall -q src` | 此前存储迁移批次成功 |

最终全套包括 PostgreSQL outbox、SQL 翻译、真实 Valkey 联动、协议路由、短码字典、AGENTS/真实 msg-entry 技能、/tools/ 与新建帖子/回复规范路径及授权后只读跳转、GET-only token/bootstrap 标量写和 1 KiB transfer.part_put 测试。此前 HTTP/字典/入口等定向检查已包含在全套中，不再次相加。

真实组件覆盖 PostgreSQL 事务回滚、并发写入、审计追加、持久 outbox、备份恢复，以及 Valkey 发布订阅和连接失败；Git 使用临时 bare 仓库，签名和客户端加密使用实际密码库。conformance 覆盖现有协议适配器的分片流程及 tokenizer 预算。本批已验证 /-/ 下 POST、签名 GET、MCP 路由与旧写入口拒绝，短码最小目录/分级详情、语义快照，以及新安装 /AGENTS.md、/.agents/skills/msg-entry/SKILL.md、/tools/（0500、tool.use）和新建 post/reply 的 .md 路径，以及授权后只读 308。

**这些通过结果不等于阶段 1 全部完成。** GET-only token/bootstrap 标量写已纳入本组全套；单业务值最多 4096 UTF-8 字节，默认原始 URL 路径上限仍为 8192 字节。当前 token 在原请求重放时可再次交付，不满足严格一次展示；没有本地随机材料的 bootstrap、复杂嵌套输入仍缺。308 仅将已有 .md Post 的旧式无后缀 URL 转到规范路径，私有资源先授权、不泄露目标；旧数据库中真正无后缀的存量 Post 没有自动改名或别名迁移。当前仅提供入口技能，不代表完整技能/局部规则发现已完成。短码仅覆盖顶层 enum/const，嵌套字段/preset 与完整 compact/normal/proof 投影仍待补齐。托管身份、分享和其他后续能力也不能据此称为完成。

仓库目前没有 `docs/verification/`、JUnit 汇总、构建日志或 package-smoke 文件；以上为本轮命令结果记录，不提供不存在的证据链接。早期“Python 3.13、101 + 5 用例、SQLite”的验收属于旧实现，不能作为本轮 PostgreSQL/Valkey 证据，也不能与本轮结果合并。

## 2026-09-27 ChatGPT 文件夹最新版需求对齐（本地与 CI）

重新从 Google Drive 的 `ChatGPT` 文件夹读取《msg.lmm.best｜项目设计》，本批依据其 2026-09-26T21:46:43.426Z 的 01–16 章版本。本批已提交为 `4c4b377`；本批[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36274644099)结果为 success，不再引用上表旧提交的 CI 作为本批证据。

| 实际命令 | 本批结果 |
| --- | --- |
| `uv run --no-project --python 3.15 --with-editable . --with pytest --with pytest-asyncio pytest tests -q` | 165 passed in 89.20s |
| `uv run --no-project --python 3.15 --with-editable . --with pytest --with pytest-asyncio --with tiktoken pytest conformance -q` | 8 passed in 31.47s |
| `uv build` | 0.1.0a1 sdist 与 wheel 构建成功 |
| `python3.15 -m compileall -q src`、`git diff --check` | 通过 |

提交 `4c4b377` 的远端环境为 Python 3.15、PostgreSQL 16 与 Valkey：测试 **165 passed in 175.58s**，conformance **8 passed in 44.22s**，sdist 与 wheel 构建成功。该结果证明本次提交的 CI 通过，不表示已发布、已部署或全部需求完成。

新增覆盖：旧 `/!`、`/~`、`/run/j|gz` 与 `/mcp` 执行别名移除；协议结构段拒绝编码别名和点段；非 `/-/` 的固定读取入口要求注册操作为 read。真实 `git-upload-pack` POST 返回 PACK，前后 refs 和关键业务表不变，`git-receive-pack` 被拒绝。`tool.run` 替代 `tool.invoke`，旧短码只保留 deprecated tombstone；`/-/transfer` 是复用六个现有 Transfer 操作的完整 OperationRequest 最小入口，`/-/d/<namespace>/<operation>` 增加分级详情。以上不证明完整 LFS、原始字节流入口或全部第 15 章 feature 验收。

## 尚未通过的验收

- 最新云盘需求与源码的差异见 [实现范围](IMPLEMENTATION_STATUS.md)，逐阶段出口见 [迭代计划](ITERATION_PLAN.md)。缺失功能不能靠现有测试数量抵消。
- 工具 runner fixture 不证明真实 bubblewrap 文件系统与网络隔离；需要真实公网、私网授权和重定向场景。
- SSH 命令解析、凭据和真实 Git hook 测试不证明 sshd 登录握手、禁止转发或宿主隔离。
- 邮件本地状态测试不证明真实 SMTP/TLS 投递；Webhook 尚无完整实现。
- 根管理内部测试不证明物理控制台身份与根目录权限；不能为测试关闭 local_only。
- 仍需要部署环境中的备份恢复演练及独立上线验收。本轮没有运行压力基准或独立安全审计，不声明吞吐量或安全覆盖率。

后续记录应同时包含代码提交、准确命令、环境版本、结果和可访问日志；区分单元/集成、协议 conformance、宿主验证及线上验证。disabled/skip 必须说明原因，不能记作 pass。正式功能必须在 CI 启用配置中执行。
