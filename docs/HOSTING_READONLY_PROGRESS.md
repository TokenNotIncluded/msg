# Hosting 只读角色推进笔记

Refs #161 #80 #83 #85；PR #174，分支 `fix/hosting-readonly-161-20260929`。基线 main `261c8816cd581deaf69fc0c810b8fa6dad5ead5e` / tree `6171f6041ffd0addaf72eadd35acf562f478281f`。协调登记 #83 comment5887036549。

## 实现边界

独立 HostingRuntime；共用注册表安装顺序/签名契约/认证授权，去掉 operation 和 manifest 的可执行回调。不构造 Application、执行器、签名器，不读服务私钥，不自动迁移、创建存储目录或同步规则。PostgreSQL/Git 明确只读模式，独立 OS 用户和 SELECT 账号；启动检查实际权限（含角色切换、列写、序列、所有权、SECURITY DEFINER），而非只设一个 read-only 默认值。

内容文件组共享仅管理员显式配置的 setgid+group-read 目录启用，binary 发布使用目标组；未配置仍0600，通用 durable_write 秘密默认0600不变。部署过程在 `deploy/HOSTING.md`。daemon 只窄接线，Application 仅抽共用装配，不动凭据交付或 executor/batch。#168/#170/#171/#172/#155 并行分支不覆盖，串行组合必须刷新 main。

## 云端证据（真实结果，不以提交代替验收）

1. 红基线 `00ba8f79cb25316bc33690f5fde96e2546176b21`；checkout `03279378a3f1e003ae1fe7fce7681f4e6efc923a` / tree `4175b4a83312d87b1d3617c6caa8775c76cc8095`。run36547405429 / artifact11023705221，Python3.15.0rc2、attempt1，**7 failed /6 passed /0 errors /0 skipped**。原6项回归全过；新增7项因缺少只读角色失败。ZIP SHA256 `a13f0ceca4d7fab74d83de71e8254daf52560e4b96bcfa21a1e0de56dad0296d` 已核对。评论 #174 comment5887238998。
2. 首版 `0f0e13d1fe7d152a9034b4028ae2e1e7e2d06966`，run36548624138，12个 fixture error（require 导入遗漏）。不算功能验收。`b1ab06c723a3bacd6c1a4ee8abddb54cbf4be31c` 修正导入后 run36549575056 为 **2 failed /11 passed**：PG 查询优化器可能在 relkind 过滤前调用 sequence-only 函数，已改 CASE 保护。
3. 扩展测试头 `eafea15af0b8ff26c0208d5c0fcc039b4d32fb56`；checkout `3b55d35391d3310c01b05393aaca5ad09343fd1b` / tree `c0e069fe69982f852859dd7224850443e5d3033c`。run36549985920 / artifact11023909276，**20 passed /2 failed /0 errors /0 skipped**。ZIP SHA256 `e7d029ca7a3621cbf8eef4c0ba3e461d6aab8e07b4b72b5ba54984d7d4c38164` 已下载核对。失败为测试把现有 forbidden_host 的403写成404，以及 fixture把libpq键值DSN直接传给只支持URL/service的配置器；不是读取私钥或越权成功。`fd8684b06e8dde33484330e1b0807503a35127a1` 改用真实PGSERVICEFILE并纠正状态码断言。

`bb3c159138c36473537e9be76aa2e59973c37285` 补内容组发布；本提交增加密钥撤销、列级/序列/所有权、0640显式开启且秘密仍0600的回归。真实 UID 用例仍须读取本次云端报告后才可标绿，之前的配置错误不冒充权限缺陷的红证据。

## 验收与剩余

所有项目执行在 Actions，本地只文本/静态审查和下载产物读取。专项保留 source.json+JUnit；完整 CI 仅补 acl 工具，不删用例，不改四分片/完整node-ID集合/conformance/构建门槛。辅助源码物化仅独立 ci 分支，产品提交不包含辅助脚本。

尚未合并或关闭 #161；须本次专项、四分片及其他现有工作流全绿并核对最终组合树。没有生产部署、权限变更、Root/PIN、资金、真实外发、备份销毁或恢复 promotion。#64/#65/#84 的现场材料缺口不通过本项关闭。
