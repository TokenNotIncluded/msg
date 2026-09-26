# 迭代路线与验收出口

权威来源为 ChatGPT 文件夹唯一[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，2026-09-27 实时复核修改时间仍为 `2026-09-26T22:52:52.714Z`，01–15 章。当前基于 `4338035` 的工作树尚未提交；本地 206 passed、8 conformance、uv build 成功，未声称本批 CI、发布或部署。

## 本批已形成的切片

- 主配置 msgd.toml、独立根目录和新安装存储布局。
- `/_test/<run_id>/` 隔离 Test Root 三级 CA 正链及部分越权/撤销负例，保持真实发布校验和 local_only。
- self-custody 的 I AM NOT HUMAN R1–R5、独立 Grant/ceremony 持久事实与签名审计。
- ReadQuery/PageCursor GET 切片、字段裁剪和可直接请求的 continuation。

这些切片不能代替完整 feature。具体边界见 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)，实际验证见 [VERIFICATION](VERIFICATION.md)。

## 下一轮按风险和依赖交付

1. **收尾 CA 自检。** 补逐级 scope/operations/issue_grants/TTL/constraints/target_service 收缩与任一级撤销；补 OnlineIssuer 有效来源、过期/撤销、同权/收缩续签及合法 pending/非法拒绝矩阵。每个失败同时验证无证书落库、CSR 未错误 issued。保持全部测试主体/CA/资源位于独立实例测试子树；不读取生产根。
2. **补成就底座的展示与事件能力。** 通用可信 Event evaluator 去重、Spec/Issuer 默认注册、Profile pin/unpin/reorder、用户 achievements 路径与公开 by-achievement 索引。先做可验收小批次：重建不重发证、隐藏不删事实、证据不泄漏、荣誉不进入 capability/Authorizer。完成默认空集合/样例/doctor/selftest；不可用安全 CA 代发荣誉。
3. **实现 custodial identity 后补 ceremony 托管分支。** 加密持钥、受控代签、token 一次展示与升级保持 subject；之后实现 R5 的 custodial 来源标记。默认每轮 60s、总 300s、服务端计时；不预填自我声明，不为通过而替主体回答。该分支与完整第 15 章矩阵完成前，不标 i-am-not-human feature 完成。
4. **扩展统一读取。** GET collection 切片后补跨协议 ReadQuery、嵌套 expand 独立分页、查询成本/超时/大小限制；再补固定 Revision 的 ReadCursor、独立 SyncCursor、Bookmark 和其他稳定索引。CLI 默认受限窗口，显式 --limit 在预算内跟随，只有 --paginate 读到结束；每页重验当前权限。
5. **继续权限与内容依赖。** 组完整生命周期→ShareGrant/ShareLink→private Notes/Todos；file/post patch、grep、安全 rebase、原子 batch 与线程；之后补 Inbox/Outbox 全来源、邮件/Webhook，TUI 只复用公共契约。
6. **单列宿主与上线出口。** Git/LFS 专用 `/-/git/<repo-id>` 写入口和 read_url/push_url；同域 hosting 的 CSP sandbox/preview/deploy/readback/rollback；真实 sshd、bubblewrap、SMTP/TLS、浏览器和物理控制台。存量迁移与备份恢复先演练，不能以新安装通过推断生产安全迁移。

每个 feature 遵循失败测试→实现→重构，并补齐确定默认值、样例或 empty/disabled/deny、doctor、自检与启用配置 CI。PostgreSQL 保存唯一业务事实，Valkey 只可选唤醒；不另建工作流引擎。提交、远端 CI、发布和部署分别记录，不复用历史结果。
