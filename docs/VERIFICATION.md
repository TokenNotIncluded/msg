# 本地与 CI 验收记录

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
