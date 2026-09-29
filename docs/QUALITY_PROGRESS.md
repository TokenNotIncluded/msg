# Python 3.15 质量与部署收口 · 2026-09-29

当前入口 PR #185，起点 main `25cf518a` / tree `61cc47b7`。上轮补丁
SHA256 `118915fe14d589cf9f1b6e512f528c3a4af1fe2b76c8bdf3aab8d9930d955798`
精确匹配基线，全部本地修改保留。当前未部署生产。
发布与现场交接入口见 [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md)。

## 当前实际证据与修复

- 第一轮 e5238e77/tree874f78ed，Python3.15.0rc2、Ruff0.16.9、uv0.12.20。
  质量运行36620632240实际15项诊断（14 I001、1 C420），stderr为空；
  ZIP SHA256 `2894295a48ac07ad83bb60c68d32168cf9d0ae1e28efab7f760fb9d854b72059`。
  source.json及全部安全建议补丁已独立核对并应用，没有unsafe fix。
- 完整运行36620632032八分片均在collection失败，37项ImportError共同原因是
  Ruff清理删掉msg.plugins.money._post_entry兼容导出。Storage36620631860
  同因4项collection error，Recovery36620632239在全量collection拒绝。
  没有将这些运行计为通过或完整执行。
- 已恢复_post_entry、_amount和Delivery的_package为明确兼容导出，继续指向
  唯一market owner；既有完整同对象矩阵、真实账本和receipt回归保持。
- 新增Deployment rehearsal：源码逐字节wheel/sdist、uv.lock导出的server哈希锁、
  干净安装、checkout外实际daemon启动/重启、四传输只读、签名客户端与幂等、
  doctor只读、默认禁外发worker及已有真实OpenSSH/Git/撤销脚本。
  全部使用可销毁回环实例和独立Test Root，不访问生产或真实收件人。
- 第二轮06d5e803/tree6bba0332：Recovery/Storage及工具、客户端、hosting、协作
  专项已通过。部署36621959536的干净server安装、启动/重启、签名客户端、
  四传输/doctor只读和worker步骤通过，真实sshd在认证阶段失败，未计为全通过。
  将隔离daemon的PAM策略对齐现有部署配置，并补认证诊断与直接key lookup。
  质量36621959251实际1个I001，已核验对应tree并应用安全格式补丁；
  ZIP SHA256 `f3bd6265b6a7d657af3bf5d40269afaecc71af919fbb62a732a1f470d4d2aafe`。
- e96e812b/tree91420337 的质量36622907866已通过，独立核验Ruff JSON为空、
  两份stderr为空、581文件format通过；ZIP SHA256
  `915bc4e4c855a2a71caf83f3e09877bff71bad348eeb2643258c249fc2f81300`。
  部署36622907569实际通过9项installed-server及7项真实sshd/Git检查；
  ZIP SHA256 `57c366c56b39edd07becd293060ec3966ce86561e7dbf8f6481afe1505dafd4e`。
  source tree、wheel/sdist和server依赖锁摘要均已读回比对；不冒称全量测试已完成。
- 补齐同一wheel的locked client-only安装和无dev依赖的正式selftest步骤。
  这两项仍须在包含步骤的最终head执行，不能借上一轮安装成功直接记通过。
- e96e812b全量运行36622907534已实际收集2518项，但八分片均失败：
  8个receipt effect-order参数例在hosting预探测业务数据时返回500；
  旧SQLite夹具仅提交事务而未关闭连接，严格ResourceWarning门禁报告泄漏。
  将receipt只读effect门禁提前到hosting探测前，保持外层runtime freshness优先；
  旧夹具显式closing并保留提交/回滚语义，SQLite连接初始化和backup目标打开
  失败也关闭已取得连接。补回归验证stale runtime不触达路由或hosting，
  以及连接初始化、目标打开和复制失败的完整关闭。未放宽警告或已有断言。

最终头仍须通过fresh Ruff/format/compile、精确node-ID八分片互斥并集、
conformance/实际tokenizer、真实wheel/sdist/干净安装及适用专项；新失败继续修复。
实际日志链、旧快照来源/冻结、新Root真实控制台、独立current pin、备份退役和
目标机容量/持久性证据分别属于#64/#65/#68/#69/#70/#84，不能由隔离CI替代。

## 上轮本地证据归档

以下内容只对应提交前的本地交付阶段，不是当前分支状态或新CI结论。

## 当前状态

本地工作区基于已经合并的 main：
`25cf518a64ca2b6af820367caa4f5bb46dbc2f02`，
原始 tree 为 `61cc47b7abc25ccdcf163d59804e323ea6a10649`。
已从已有 Git bundle 恢复并核对远端已发布 commit/tree 的准确哈希。
本轮没有创建业务提交、推送、触发云端工作流或执行生产部署。
所有代码修改均留在工作区，HEAD 仍是以上基线。

此前质量候选只作为本地修复来源，没有套用相对旧 head 的补丁：
先重建准确 main，再重新生成相对该 main 的差异。
Ruff 的自动修复/格式化来源是此前真实云端运行；后续人工修改
和本轮修改尚未经过最终 Ruff 0.16.9 验证，不能标记为零诊断。

## 本轮新增修复

- `src/msg/__init__.py` 的运行时版本从错误的 `1.0.0a1` 对齐至发行版本 `0.1.0a1`。
- 构建校验从仅检查资源/命令名称扩展到完整包文件集合、逐字节 Python 源码、
  Name/Version/Requires-Python、准确 console target、sdist 当前源码和锁文件。
  旧代码、额外的已移除模块和错误元数据不能再被校验脚本当作合格产物。
- 本地 PostgreSQL 测试显式创建并使用 `msgtest` 临时角色，不依赖开发者的系统用户名。
- 安装测试 fixture 使用 finally 回收应用和临时数据库；初始化失败也清理，
  session 级 seed 数据库有明确 teardown，不再永久残留在配置的测试集群中。
- `scripts/verify_local.py` 提供本地检查入口，记录实际未提交文件指纹、退出码与日志；
  缺依赖、非零退出、要求清空的 stderr、跳过的 conformance、源码漂移都不会标为通过。
- uv 的版本要求写入 `pyproject.toml`；旧 uv 的真实拒绝结果已记录。
- 收拢重复的显式 import，保持导入源、名称、别名及绑定顺序，减少 695 行展开代码。

## 实际运行结果及范围

现有可执行环境是 Python 3.13.5、uv 0.10.0，不是目标环境。
新增发布回归先得到 8 个失败/2 个通过；修复后通过。
新增 fixture 生命周期回归先得到 4 个失败；修复后通过。

最终本地补充回归：54 passed，0 failed / 0 error / 0 skipped。
包含 29 个既有 CI 分片校验测试和 25 个新增工具/配置/fixture 编排测试。
测试通过 `--noconftest` 运行指定的五个不依赖真实数据库的测试文件；
fixture 生命周期测试使用模拟进程/应用检查编排，不是 PostgreSQL 实测。
产物校验测试使用合成档案，不是本项目真实 wheel/sdist 构建成功的证据。
没有把这些结果当作 Python 3.15 全量业务测试。

附加静态源码审查没有发现未绑定的全局名称或额外语法错误。
该审查仅为了让 Python 3.13 的解析器读取源码，临时规范化了
18 个文件中的 Python 3.14+ except 语法，没有修改生产文件，也没有运行这些模块。
它不代替 Ruff、目标版本编译或业务测试。

固定旧版源代码 `ac083b3666b1b5268d90115129e2cc4e0dd11521`
已恢复至本地 `.legacy-ledger`，按既有 manifest 验证了 8 个文件的 SHA-256；
没有因此宣称数据库迁移/回滚已执行。

## 仍未完成

本环境没有 Python 3.15、Ruff、build、psycopg、graphql-core、valkey、
tiktoken 和若干系统测试工具。pip/uv 下载及 GitHub 域名解析未成功。
`verify_local.py --preflight-only` 实际退出 1，结果是 blocked。

因此最终 Ruff/format、锁定环境安装、Python 3.15 全量测试、真实
PostgreSQL/Valkey/Git-LFS/age/bwrap 验收、conformance/tokenizer、
真实 wheel/sdist 和干净安装仍未完成。当前不能标记为可部署或全部完成。
生产日志、旧库、Root、本机控制台、恢复授权和备份退役仍有各自验收边界。

本地执行入口及步骤见 `LOCAL_VERIFICATION.md`。会话交付包的 evidence/
保存真实日志、JUnit 和环境阻塞记录；不借用此前其他提交的 CI 成功。
