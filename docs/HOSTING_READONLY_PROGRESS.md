# Hosting 只读角色推进笔记

Refs #161 #80 #83 #85。分支 `fix/hosting-readonly-161-20260929`；基线 main `261c8816cd581deaf69fc0c810b8fa6dad5ead5e` / tree `6171f6041ffd0addaf72eadd35acf562f478281f`。协调登记 #83 comment5887036549。

## 边界

主责 hosting 装配、必要只读存储模式、部署模板与测试。daemon 仅窄接线，Application 仅抽取共用登记装配，不动 #172 凭据交付、#167 执行器/存储会话语义。#168 市场、#170 Git、#171 客户端、#155 恢复的分支不覆盖；共享文件最终组合仍须串行审查。

## 本次提交：测试先行

新增真正 PostgreSQL 只读角色 fixture、无服务密钥启动/禁止规则同步、Registry 契约元数据与不可执行回调、数据库写入口/真实 SQL 拒绝、Git 读取/写入拒绝、错误 writer DSN、缺规则不修复、独立 OS 身份模板检查。仅测试和工作流，尚无生产实现；预期红基线须读取实际云端报告后记录，不能提前记为已验证。

`Hosting read-only role` push/PR 工作流运行 Python 3.15、真实 PostgreSQL/Git/age，保留现有 hosting/CLI 回归，上传 source.json 与 JUnit。项目代码不在本地执行；本地只做文本、差异、源码和下载产物检查。完整四分片/node-ID/conformance/wheel/sdist 门槛不被专项替代。

## 后续

实现仅认证/当前授权/读取的运行上下文；不加载在线/回执/token 私钥，不持有 Application/写执行器，不初始化数据库/目录或发布规则。数据库专用 SELECT 角色与 OS 只读权限分别验证；私有 preview、当前撤权、HEAD/Range/暖缓存、恢复 quarantine 继续拒绝越权。

没有生产部署、权限变更、Root/PIN、资金、真实外发、备份销毁或恢复放行。#64/#65/#84 等现场材料缺失不通过本项关闭。
