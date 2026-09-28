# Issue 推进笔记

本轮入口：2026-09-28T20:34Z 用户请求。源码、测试证据、合并及现场验收分开记录；不以局部绿灯关闭总体验收。

## 本轮交付入口

PR #167 集成 #156（执行器边界）和 #157（存储会话边界）。两组独立红基线与实现历史均保留，只在同一集成头做最终验收，避免分别合并后重复跑全量。

基准 main：476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7 / tree40ca207a3c49be347120e058e47ed328ec4e3098。该基准 CI36472507327 和 Recovery safety36472507300 已成功，不代表本次新头通过。

已阅读权威设计、有效完成归档、仓库贡献规则与当前源码。最终运行状态与合并结果以 PR #167 最新验收评论为准；下列证据各自绑定明确的历史头，不互相冒用。

## #156 执行器与公共协议

已实现：固定 ResultProjection、TransactionalEventNotifications 接口；Application 同一工厂装配 HTTP/SSH 的请求上限、秘密响应释放和恢复隔离。executor 不再持有整棵 Application，也不导入插件内部函数。

core.batching/core.events/core.packet/core.schemas 各自只有一份逻辑；原公开导入路径重导出同一对象。URL秘密拒绝和gzip规则仍在传输层；签署字节、确定性事件ID、批处理排除项、当前权限、回放、事务和receipt规则保持。

红基线：453d6f858e19136261278c7042c13598035aef1a / treeae0b4f5e31dc1844c1abd0142da898b75b7779d4。

- 云端36481310584，job109127358396：165 tests，7 failed / 158 passed / 0 errors / 0 skipped。
- 七个新增回归全部失败，原有158个用例全部通过。
- artifact10997043729已下载核验，ZIP SHA256 c2e1978649b6e251e38703befb0fc674fbb336db55fa075113af0edb07d81fb7。
- source.json匹配上述head/tree，Python3.15.0rc2，attempt1。

源码实现10f46fd300ef0fa4890ca0ea7aad3d10bca7f7a5 / treee9e33a3286f6d4efd19a421834bba813448f6a79；并发与笔记补充20b6d7e3c6c2d22a8f9e1ae7f2f89e1a1812dc27 / tree40e3f60f6689435b2bfc39e6c7c63f115beadd16。独立头的运行结果不可替代组合头验收。

## #157 存储会话与真实接口

已实现：PostgresSession 和 SqliteSession 同为 RelationalSession 的子类；共用域方法不再持有连接或导入数据库驱动。两后端各自保留连接、SQL转换、schema迁移、隔离、写栅栏、嵌套事务、回滚补偿与提交后信号。

QuerySession明确execute/one/rows/setting/set_setting，MetadataSession继承该接口；补齐真实跨模块使用的path/ancestors/organization/job/save_job。check和run_rollback_effects仍属存储生命周期实现，不作为调用者权限。

execute返回受会话约束的结果视图，不公开驱动connection/commit。rowcount、fetchone、fetchall与迭代重新检查任务归属和事务生命周期。没有增加ORM、逐表Repository或任意SQL网络接口，也不把进程内Python视作安全沙箱。

红基线：2daf1e0b81783d8b52685540b041ba9563333282 / treee1668ddcbf5879bce9520a31f8bfe911c813370c。

- 云端36482022051，job109129719949：43 tests，8 failed / 35 passed / 0 errors / 0 skipped。
- 八个新增回归全部失败，原有35个用例全部通过，包含真实PG和SQLite。
- artifact10998115401已下载核验，ZIP SHA256 fe037acb748babcb05dcdc7c679bb42986f7ae509af86612ce510307c90ed3e3。
- source.json匹配上述head/tree，Python3.15.0rc2，attempt1。

源码实现4a6b852961b5e9745c1fea8d37117a7a7c1bde58 / tree32e13775b0e2e0e85d240bebccae8fd2d56adfa8；组合提交再补齐五个域接口与更强的政策消费者回归。

SQLite是精简测试后端，不是完整市场生产bootstrap。消费者测试的SQLite隔离fixture复用生产_SCHEMA中的arbitration_policies DDL；不修改生产迁移、不声称全市场后端等价。原断言全部保留，另增加真实已存政策读取、损坏digest拒绝和公开域方法完整声明检查，不只走默认空表分支。

## 集成验收门槛

保留两个专项、完整四分片、精确node-ID全集/互斥校验、conformance、实际tokenizer、wheel/sdist和Recovery safety。CI只把同一来源仓库分支的push/PR归到同一并发组，减少重复runner，不删用例、放宽断言、减少依赖或取消合并门槛。

所有项目执行放在GitHub Actions；本地只编辑源文本、检查Git差异/树与读取下载证据。临时源码导出/发布脚本与工作流均只在work/issue-workspace-20260929，不进入产品分支。发布阶段验证脚本SHA256和完整预期Git树，并只写本会话暂存分支。

本笔记提交时，最终组合验收尚未完成；不标已合并，也不据此关闭#83/#85。最终证据在PR #167验收评论中逐项登记。

## 并行协作与剩余项

- #155恢复分支0e4dbe777053e354bcf4845b851a0c9b86ab8310：专项36480031762成功，完整36480031758四分片/conformance/build/node-ID gate成功；需与最新main冲突处理后的组合验收。本会话不覆盖该分支。
- #160由fix/architecture-boundaries-20260929处理；#159已出现PR #168市场边界回归。本会话不重复写这些分支。
- #158客户端/服务端依赖、#68–70恢复完整验收、#71–72市场矩阵、#76–82各功能验收、#83配置/Registry/逐条证据、#85其余模型收敛仍按对应issue/PR跟踪，不因本次两个架构修复整体关闭。
- #64/#65/#84仍缺授权的生产代理/日志、真实旧库快照、容量与恢复材料，CI不能替代。#68/#69/#70独立新鲜检查点、备份退役、真实控制台及promotion必须分别验收。

本次不部署、不操作真实Root/PIN、资金、外发、备份销毁或恢复放行。
