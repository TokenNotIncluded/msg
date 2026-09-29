# 本地质量收口 · 2026-09-30

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
