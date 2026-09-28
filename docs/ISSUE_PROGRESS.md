# Issue 推进笔记

用户入口：2026-09-28T20:34Z。PR #167 是本轮 #156/#157 的唯一组合交付入口；基准 main476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7，tree40ca207a3c49be347120e058e47ed328ec4e3098。最终提交/CI/合并状态以 #167 最新验收评论为准；本文只登记已取得的证据，不用历史绿灯代替新头。

## 已完成的源码切片

#156：core.batching、events、packet、schemas 各有一份中立实现，原导入路径为同对象兼容别名；URL秘密拒绝和gzip仍在传输层。executor没有Application/插件反向引用，只接收固定投影和事务通知接口。HTTP/SSH由Application.new_executor统一装配请求上限、秘密响应和恢复隔离。签名、ID、回放、权限、事务和receipt规则保留。

#157：PG/SQLite独立继承不持有驱动/连接的RelationalSession。SQL、schema、迁移、锁、保存点、补偿和提交信号仍由后端管理。显式QuerySession覆盖真实消费者，MetadataSession补齐path/ancestors/organization/job/save_job；查询结果视图每次读取重验任务/生命周期，不公开connection/commit。没有新增ORM、逐表Repository或任意SQL网络接口。

SQLite仍是精简测试后端。政策消费者测试只在隔离fixture复用生产仲裁表DDL，验证真实已存政策及损坏digest拒绝，不修改生产迁移或声称完整市场后端等价。

## 测试先行证据（均已下载并独立核验）

| 范围 | 测试头 / tree | run / artifact | JUnit |
|---|---|---|---|
| #156 | 453d6f858e19136261278c7042c13598035aef1a / ae0b4f5e31dc1844c1abd0142da898b75b7779d4 | 36481310584 / 10997043729 | 7 failed / 158 passed / 0 errors / 0 skipped |
| #157 | 2daf1e0b81783d8b52685540b041ba9563333282 / e1668ddcbf5879bce9520a31f8bfe911c813370c | 36482022051 / 10998115401 | 8 failed / 35 passed / 0 errors / 0 skipped |

新增用例准确失败，原有选定用例全部通过。ZIP SHA256分别为c2e1978649b6e251e38703befb0fc674fbb336db55fa075113af0edb07d81fb7、fe037acb748babcb05dcdc7c679bb42986f7ae509af86612ce510307c90ed3e3；source.json匹配head/tree/Python3.15.0rc2/attempt1。

实现历史：10f46fd300ef0fa4890ca0ea7aad3d10bca7f7a5（执行器）、4a6b852961b5e9745c1fea8d37117a7a7c1bde58（存储），组合a85b74456be8eb20977ddd144b94483b0b21202f/tree042422943d4f5a9c43e5d531797dcc3d9c92a787。该历史组合存储专项36483874055已success；后续扩展回归头仍须重新验收。

## 重叠PR收敛

已对照#166和#169完整改动及用例，在#83评论5878614115、#166评论5878687666、#169评论5878694971协调，不覆盖其源分支。

保留#166全部16个结构/双后端行为测试，仅将拟议基类名改为唯一RelationalSession；补充#169的独立装配、工厂批处理回放、事件通知失败整事务回滚3个用例。签名/认证/事务和失败不提交断言未降低。#169的missing-decoder配置用例不适用于内置中立codec，不引入第二套decoder/BatchPolicy制造该错误；既有限长、秘密拒绝、投影缺失拒绝均保留。原red证据仍归#166/#169，不冒充本分支新红基线。

## 合并门槛与执行边界

两个专项、完整四分片、精确node-ID全集/互斥、conformance/实际tokenizer、wheel/sdist、Recovery safety均保留。同一来源仓库分支的push/PR共享并发组以减少重复runner，不删测试、放宽断言或降低合并门槛。最终组合通过并合并后才关闭两个已修复issue和重复PR。

项目执行全部在GitHub Actions；本地仅源文本/Git差异与树检查、下载证据读取。临时精确树发布/源码导出仅在辅助分支，不进入产品树。42个共享方法主体、两后端Store/schema/迁移的静态对照未发现改动，云端运行仍是验收依据。

## 剩余与并行工作

#165工具、#168市场、#158客户端及#155恢复由独立分支推进，不覆盖。#155已知0e4dbe7头完整36480031758及专项36480031762成功，但仍须当前main组合与本机CLI/文件边界验收。#68–83/#85各功能总体验收不能因这两个架构修复整体关闭。

#64/#65/#84仍缺授权生产代理/日志、真实旧库快照、容量与恢复材料；#68–70独立新鲜检查点、备份退役、真实控制台及promotion须分别验收，CI不能替代。此次不部署、不操作真实Root/PIN、资金、外发、备份销毁或恢复放行。
