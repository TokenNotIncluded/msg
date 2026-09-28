# Issue 推进笔记

更新入口：2026-09-28T20:34Z 用户请求。代码、回归、CI 与现场验收分开记录；不以局部测试关闭总 issue。

## 基线与协作

- main：476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7，tree 40ca207a3c49be347120e058e47ed328ec4e3098。
- main 完整 CI36472507327、Recovery safety36472507300 已返回 success。
- #155 独立推进到0e4dbe777053e354bcf4845b851a0c9b86ab8310，修正原验签导入与换行语法；专项36480031762 success。尚未独立核其产物；完整36480031758与主线冲突另核。
- 本分支 fix/executor-core-20260929-2045 不写 #155/#145 原分支。只读源码导出工作流 work/issue-workspace-20260929 不进主线。
- 已阅读权威设计、有效完成归档、仓库贡献规则及当前源码。#83旧正文不作为当前提交状态。

## 本批次 #156

- [x] 核对依赖：executor 反向导入 batch/discovery/communication，持有整棵 Application；packet 依赖 plugins.schemas，共同协议的所有权分散。
- [x] 先提交独立架构及真实签名/投影/回放回归。
- [ ] 云端记录失败基线（不是本地运行）。
- [ ] 单一批处理/事件标识函数所有权；明确窄投影与事务通知接口；HTTP/SSH 同一装配配置。
- [ ] 共用请求/结果、基础schema和wire codec中立化；URL专用限制仍由传输层负责。
- [ ] 云端修复后聚焦回归、全四分片/精确node-ID/conformance/build/Recovery safety。
- [ ] 精确头审查并合并；随后更新对应 issue。没有通过证据前不标完成。

## 尚需继续

#160由已存在的fix/architecture-boundaries-20260929分支推进，本会话不覆盖。

#157 存储中立会话；#158 客户端/服务端依赖；#159 市场插件依赖；#68–70恢复本机命令、当前权限与现场验收；#71–72市场矩阵；#76–82权限/个人空间/读取/内容/存储/通知/客户端验收；#83配置/Registry/逐条证据；#85其余模型收敛。

#64/#65/#84缺实际授权的生产代理/日志、真实旧库快照、容量与恢复材料，不能用CI替代。#68/#69/#70的独立新鲜检查点、备份退役、真实控制台与promotion仍必须分别验收。本次不部署、不操作真实Root/PIN、资金、外发或备份销毁。

## 当前提交进度

失败基线已提交453d6f858e19136261278c7042c13598035aef1a，tree ae0b4f5e31dc1844c1abd0142da898b75b7779d4。云端专项36481310584和完整36481310527已触发；回读时仍排队，未当作通过。

实现采用固定的ResultProjection和TransactionalEventNotifications接口，不引入任意插件钩子。core.batching/core.events/core.packet/core.schemas分别只有一份真实逻辑；旧公开导入路径兼容重导出。URL秘密检查和gzip只在传输层，未移到所有入口。Application.new_executor同时装配HTTP/SSH的请求限制、秘密释放和恢复隔离，不再给executor整棵Application。未运行本地项目测试，尚无修复后通过结论。
