# Hosting 只读角色推进笔记

Refs #161 #80 #83 #85；PR #174，分支 `fix/hosting-readonly-161-20260929`。基线 main `261c8816cd581deaf69fc0c810b8fa6dad5ead5e` / tree `6171f6041ffd0addaf72eadd35acf562f478281f`。协调登记 #83 comment5887036549。

## 范围与实现

独立 HostingRuntime 只读装配、必要 PostgreSQL/GitContentStore 只读模式、部署模板与测试。共用原注册表安装顺序，保留契约元数据，同时去掉 operation 和 manifest 上的可执行回调；仍用原认证、当前授权与托管读取，不构造 Application/执行器/签名器或读取服务私钥。启动只核验现有 schema、root、release 来源和恢复隔离，不迁移、不补目录、不发布规则。daemon 只窄接线，Application 仅抽共用装配，不动凭据一次性交付和 executor/batch 政策。

#168 市场、#170 Git、#171 客户端、#172 凭据、#155 恢复源分支不覆盖。最终串行组合必须刷新 main，专项不能替代完整四分片/node-ID/conformance/wheel/sdist。

## 已核验云端证据

测试先行头 `00ba8f79cb25316bc33690f5fde96e2546176b21`，合成 checkout `03279378a3f1e003ae1fe7fce7681f4e6efc923a`，tree `4175b4a83312d87b1d3617c6caa8775c76cc8095`。run36547405429 / artifact11023705221，Python3.15.0rc2、attempt1，JUnit **7 failed /6 passed /0 errors /0 skipped**。新增7项因缺少只读能力准确失败，现有6项全过。下载 ZIP SHA256 `a13f0ceca4d7fab74d83de71e8254daf52560e4b96bcfa21a1e0de56dad0296d` 与提供方一致；逐 node 与 source.json 已核对。证据评论 #174 comment5887238998。

首版实现 `0f0e13d1fe7d152a9034b4028ae2e1e7e2d06966` / tree `6c34817242491b2631899fd3cce7dba69d7915d8` 的 run36548624138 出现12个 fixture error：PostgresMetadataStore 遗漏 require 导入。此轮不作为功能通过或红基线证据。补丁 `b1ab06c723a3bacd6c1a4ee8abddb54cbf4be31c` / tree `667462eeb5968a8728d57960e988f7972fcf3fbb` 已修正导入并触发 run36549575056；最终结论仍待真实报告。

## 本次补充验收

增加真实签名 private preview/ACL撤回（含 HEAD、Range、ETag）、错误 Host、不暴露 schema、持久/文件/悬空符号链接 quarantine、缺 public trust/schema 不修复、DB额外写权限/SET ROLE/SECURITY DEFINER 拒绝。

真实 UID65534 子进程不能读 fixture online.key 或写业务文件，但必须读公开页面与后续新发布的文本和二进制 preview。只在发布前配置内容组权限，发布后禁止靠 chmod 修复用例。已发现原 durable_write/mkstemp 的0600可能使新文件不可读，将以云端结果定位；不能全局改 durable_write 默认值（该工具也写秘密），内容组共享需显式配置。

工作流补齐 acl 依赖，完整 CI 只增加此工具，不删测试、不改四分片或 gate。项目执行全部在 Actions；本地只做源码文本/静态审查及下载证据读取。辅助 git 补丁生成仅独立 ci 分支，产品提交不含辅助脚本。

尚未合并或关闭 #161。没有生产部署、权限变更、Root/PIN、资金、真实外发、备份销毁或恢复 promotion。#64/#65/#84 等现场材料缺失不通过本项关闭。
