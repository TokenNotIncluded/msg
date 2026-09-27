# Issues #78–80：实现进展与验收边界

日期：2026-09-28（Asia/Tokyo）。状态：**本批实现进入主线合并流程；#78–80 尚未完成全部验收，issue 保持开放。**

## 源码与验证基线

仓库为 `TokenNotIncluded/msg.lmm.best`。最终补丁已在 main 提交 `ad256d037ec4febbee95f03b7044f13b9eebb485` 的 CI 源码发行包上应用并复测。源码来自 workflow run `36345120750`、artifact `10941275291`，ZIP SHA-256 为 `44dfd4a21933c7861b7563749d85cae48273c44b2061a53fb9cf00274d655732`。这是由 CI sdist 重建的源码/测试快照，不是完整 Git 历史；上游 CI 成功不代表本补丁通过 CI。

本地 Python 为 3.13.5，项目生产基线为 3.15。新增测试使用仓库支持的 `FakeMetadataStore`（事务性 SQLite）、真实 Registry、Authorizer、AuthenticationService、OperationExecutor、Git 文件存储和 HTTP/PathGET/MCP 适配器。没有把 SQLite 结果当作 PostgreSQL 并发结果，也没有模拟缺失的数据库/GraphQL 依赖来宣称全套通过。

新增目录 `tests/issue_78_80/`：**103 项通过，0 跳过**。其中结构化 patch 46 项、LFS 19 项、多层读取及传输 38 项。两项确定种子的 unified-diff 测试额外各执行 150 组往返案例；这些循环不另外计入 pytest 项数。

```sh
PYTHONPATH=src python -m pytest --confcutdir=tests/issue_78_80 tests/issue_78_80
python -m compileall -q src tests/issue_78_80
git diff --check
```

`--confcutdir` 仅用于运行这套明确隔离的测试，避开完整仓库根 conftest 对 PostgreSQL 的导入；没有修改、禁用或降低原有 CI 测试。完整仓库测试必须在完整依赖环境中照常执行：

```sh
python -m pytest tests
python -m pytest conformance
python -m build
```

本机全仓测试在收集阶段因缺少 `psycopg` 中止；也缺少 `valkey`、`graphql-core`、`build` 及生产 PostgreSQL 服务。上述完整测试、构建、doctor/selftest 与远端 CI 均未作为本次通过项。

## #78：多层读取与当前权限

新增 `discovery.read_query@3`，不把新能力自动加入已发行凭据的 operation ceiling。`expand` 使用有限的对象树，每个 `children` / `replies` 节点独立指定 `limit`、`fields` 和下层 `expand`，独立返回 `pageInfo.endCursor`、`hasNextPage`、`next`。

```json
{"parent":"r_topic","limit":1,"fields":["id"],"expand":{"children":{"limit":1,"fields":["id"],"expand":{"children":{"limit":1,"fields":["id"]}}}}}
```

对应可读纯路径，不要求把完整查询编码成 Base64：

```text
/_r/q/3/r/r_topic/n/1/f/i/x/children/n/1/f/i/x/children/n/1/f/i/up/1/up/1
```

`/_read/` 为等价别名。`x/children`、`x/replies` 进入节点，`up/1` 退出节点。签名读取后缀仍为 `/p/{短期签名请求}`，签署参数含 `query_version: 3`；续读参数只含 `cursor`。复杂或需要安全传输的查询仍可经正式 Transfer 执行操作 seal 为 QueryRef。查询字符串表示使用 `version=3` 和 `tree`；后者是对象树 JSON，不要求纯路径客户端使用它。

上限为展开深度 4、总节点 100、字段计费成本 1000、总扫描 4096、嵌套每页 10；保留已有单集合扫描上限及请求截止时间，响应字节数受现有服务配置限制。游标绑定主体、凭据、查询、字段、稳定资源 ID、快照边界和有效期；返回前按当前权限复查。跨 v3/v1-v2 游标降级被拒绝；兼容已有 v1 和无 expand 标记的旧 v2 平页。

HTTP、纯路径 GET 和 MCP 客户端实测复用同一个鉴权器/执行器；覆盖别名、HEAD、ETag/304、独立续页、撤权与凭据撤销、过期/超长 proof、token URL 拒绝及数据库所有业务表前后完全相同。短期签名 URL 在有效期内允许被完整复制重放；测试明确保留这个事实，不宣称 URL 本身提供强响应保密。token-only 客户端不能因此获得把 token 放入 URL 的权限。

QueryRef 复用同一 v3 查询逻辑，不授予权限。其根页使用短的 QueryRef continuation；最后一页可以有 endCursor，但不会因此伪造 `next`。源文件失去权限、凭据 ceiling 收缩时立即拒绝。另测 9 KiB 查询经乱序分片后 seal、续读时不回显长查询。

**尚未完成的 #78 总验收**：全部正式读取入口和所有字段的完整矩阵、真实 GraphQL/CLI/TUI 全路径、token-only 的接收者绑定纯 GET 加密方案、超过 64 个已知引用的 Sync/resync 窗口、所有搜索/facet/spell/关系条件与附件/Range 导航矩阵。不能据本节关闭 #78。

## #79：结构化 patch 与独立 Revision 签名

新增 `content.text_patch@3`、`content.post_patch@2`、`content.text_patch_batch@2`。保留既有版本及 batch v1 的 `data.generations` 返回格式。新输入含 `id`、`base_revision`、`base_generation`、`patch`，并可携 `rebase`、`change_note` 与独立签名字段。

`patch.kind` 支持 exact、单文件 unified diff、heading、block。heading/block 必须带完整 SHA-256 摘要；heading 匹配原始章节，block 匹配原始文本块，不以短 hash 猜目标。歧义、错误计数、错位、目标修改、不可确定插入或多目标匹配均拒绝。rebase 先验证原基线，再要求当前目标及上下文未变且唯一。

Markdown 定位器是有界的原始文本段落/ATX、setext 标题/围栏代码块定位器，**不是完整 CommonMark AST 或语义重构器**。文本、patch 和输出均限制为 1 MiB；保留 Unicode、CRLF 及无末尾换行。限定 hunk/行数及匹配成本。

`msg.client_content.signed_patch_arguments` 用现有客户端 Signer 独立签署 Revision manifest；请求签名、Revision 签名、服务 receipt 不混为一谈。新 Revision 固定 change_note 和 patch provenance，旧 Revision 字节及签名不修改。批量先验证全部目标，再在执行器拥有的 SQL 事务内发布；中途独立签名失败会回滚全部资源指针、Revision 和投影。可靠 put 后产生的无引用 Git/CAS 对象可以残留，不能因此宣称完整 GC 验收。

**尚未完成的 #79 总验收**：所有文件/帖子生命周期组合、所有写法的客户端签名普及、模板/发行规则的完整来源矩阵，以及 Markdown/HTML/TUI/附件/Range 全部表示的授权一致性。不能据本节关闭 #79。

## #80：LFS 摘要校验与显式旧布局迁移

新增 `git.lfs_migrate@1`，输入仅为仓库 `id` 和 SHA-256 `oid`，经过普通签名鉴权、当前仓库写权限、状态与已持久化容量配置检查。知道 digest 不构成迁移授权；不接受客户端宿主路径。迁移是显式执行操作，stat/GET 不暗中改写旧布局。

LFS 发布逐字节校验暂存源、已有仓库对象及已存在的共享 CAS 对象，拒绝同大小但摘要错误的内容、符号链接与非普通文件。通过同目录临时硬链接和原子 replace 将有效旧对象迁移至共享 inode；已共享对象幂等返回。原有对象在替换前失败时保持完整。

发布、迁移和本模块回收共用跨进程文件锁，锁有 30 秒默认超时，繁忙明确报错。测试使用真实 `multiprocessing` 子进程竞争最后一份 LFS 容量，并覆盖重复内容去重、损坏对象、迁移失败、符号链接、读操作不改变 inode 和 GC 引用保护。

**范围限制**：该锁约束本模块协作的 LFS 写入者，不是所有内容写入者的全局事务，也不保证阻止直接绕过应用修改文件系统的人。

**尚未完成的 #80 总验收**：普通 CAS 与所有 staging 的总预算、异常重启后的全部暂存回收、Git 多 ref/外部写者/备份一致性完整矩阵、网页托管 CSP/CORS/私有 API/rollback 浏览器实测、真实 PostgreSQL 与 Git/CAS/LFS/恢复材料的一致备份恢复。不能据本节关闭 #80。

## 合并门槛

三项 issue 保持开放。本批实现需要在 Python 3.15、PostgreSQL、Valkey、graphql-core、所需系统工具和构建依赖齐备的环境跑原有 CI；补齐上述未完成验收、BootstrapManifest 与对应 doctor/selftest 后，才能决定各 issue 是否可关闭。未通过当前提交 CI，不建议直接合并或部署。
