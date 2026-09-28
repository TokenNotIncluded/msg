# Issue 推进笔记

本分支 fix/storage-session-20260929-2050 处理 #157，基于 main476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7。与 #156 执行器分支、#160 工具接口分支及 #155 恢复分支隔离，合并时将进度归入同一笔记。

- [x] 阅读 issue 全文及第二轮补充：既解除后端继承，也明确真实 SQL 会话契约。
- [x] 提交双后端失败回归：独立构造、真实政策消费者、受限结果视图、只读/跨任务/关闭后拒绝、嵌套回滚。
- [ ] 云端记录失败基线；实现后运行同一用例，不降低断言。
- [x] 中立共享 RelationalSession；各后端保有连接、方言、迁移、事务和错误映射。
- [x] 显式 QuerySession/QueryResult，不给 handler 裸连接或提交权；避免增加逐表 Repository/ORM。
- [ ] 完整四分片/node-ID/conformance/build 与恢复回归；审查精确头后合并。

所有项目执行在 GitHub Actions；本地仅源文件编辑、差异与Git树检查。当前没有修复后测试通过结论，没有生产部署、真实数据迁移或恢复放行。

#64/#65/#84的生产材料缺口及#68–70现场验收仍开放。其他任务状态以对应最新issue/PR和组合head的证据为准，不以本笔记替代权威设计与有效完成归档。

## 实现切片（尚待云端验收）

SqliteSession/PostgresSession改为同一RelationalSession的兄弟类；共用域方法不再持有连接或导入数据库驱动。所有原SQL、schema、迁移、隔离、写栅栏、恢复补偿、提交后信号保留。QuerySession显式列出实际查询方法；MetadataSession继承它，真实market.policy.load_policy以该接口标注。

execute返回受会话约束的QueryResult而不是驱动游标，不公开connection/commit；rowcount/fetchone/fetchall/迭代均重验任务和生命周期。没有把进程内Python当作安全沙箱，没有增加ORM/逐表Repository/任意SQL网络接口。

实现待同一红基线用例、真实PG/SQLite、账本、回滚补偿和完整四分片验收。所有执行在云端，未进行本地项目测试。
