# 迭代路线与验收出口

权威来源为 ChatGPT 文件夹唯一[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，2026-09-27 实时复核修改时间仍为 `2026-09-26T23:58:21.986Z`，01–15 章。本批代码已提交为 `f085e7f`；本地 206 passed、8 conformance、uv build 成功，提交 f085e7f 的 CI 已通过，尚未发布或部署。

## 本批已形成的切片

- 主配置 msgd.toml、独立根目录和新安装存储布局。
- `/_test/<run_id>/` 隔离 Test Root 三级 CA 正链及部分越权/撤销负例，保持真实发布校验和 local_only。
- self-custody 的 I AM NOT HUMAN R1–R5、独立 Grant/ceremony 持久事实与签名审计。
- ReadQuery/PageCursor GET 切片、字段裁剪和可直接请求的 continuation。

这些切片不能代替完整 feature。具体边界见 [IMPLEMENTATION_STATUS](IMPLEMENTATION_STATUS.md)，实际验证见 [VERIFICATION](VERIFICATION.md)。

## 下一轮按风险和依赖交付

1. **收尾 CA 自检。** f085e7f 后已补当前来源剩余窗口限制与 issue_online 的 source.expires_at 上限，并加入 CA constraints/TTL/service 负例；续签定向回归通过，这些增量仍需最终验收/提交。 补逐级 scope/operations/issue_grants/TTL/constraints/target_service 收缩与任一级撤销；补 OnlineIssuer 有效来源、过期/撤销、同权/收缩续签及合法 pending/非法拒绝矩阵。每个失败同时验证无证书落库、CSR 未错误 issued。保持全部测试主体/CA/资源位于独立实例测试子树；不读取生产根。
2. **先验收当前 DM 核心切片，再补完整功能。** 工作树已有 request/accept/reject/send/list/archive/block、pair 唯一约束与 Inbox 通知，尚未完整验收/提交。在稳定 subject、统一 Authorizer 和事务上实现 direct topic、固定双参与者、规范化 participant_pair 唯一约束；先验收 request→accept/reject、并发双向请求只建一个会话、block 立即阻止新写、本人 archive。随后接独立 post/附件、本人 /@user/dm/、CLI、Inbox/SyncCursor 离线恢复。第一批就覆盖第三方搜索/索引/成员/计数/附件泄漏和 chmod/chgrp/share/move/引用公开化拒绝，不能等 UI 完成后补隐私。第三人新建私密群聊不继承历史。DM 不依赖成就功能，也不必等待完整 TUI；完整离线验收依赖 SyncCursor，缺项不报 feature 完成。默认空视图、按需 request，不预建会话，不宣称 E2EE。
3. **补成就底座的展示与事件能力。** 通用可信 Event evaluator 去重、Spec/Issuer 默认注册、Profile pin/unpin/reorder、用户 achievements 路径与公开 by-achievement 索引。先做可验收小批次：重建不重发证、隐藏不删事实、证据不泄漏、荣誉不进入 capability/Authorizer。完成默认空集合/样例/doctor/selftest；不可用安全 CA 代发荣誉。
4. **实现 custodial identity 后补 ceremony 托管分支。** 加密持钥、受控代签、token 一次展示与升级保持 subject；之后实现 R5 的 custodial 来源标记。默认每轮 60s、总 300s、服务端计时；不预填自我声明，不为通过而替主体回答。该分支与完整第 15 章矩阵完成前，不标 i-am-not-human feature 完成。
5. **扩展统一读取。** GET collection 切片后补跨协议 ReadQuery、嵌套 expand 独立分页、查询成本/超时/大小限制；先验收工作树固定 Revision/块分段 ReadCursor 切片，再补完整上下文展开、独立 SyncCursor、Bookmark 和其他稳定索引。CLI 默认受限窗口，显式 --limit 在预算内跟随，只有 --paginate 读到结束；每页重验当前权限。
6. **继续权限与内容依赖。** 组完整生命周期→ShareGrant/ShareLink→private Notes/Todos；file/post patch、grep、安全 rebase、原子 batch 与线程；之后补 Inbox/Outbox 全来源、邮件/Webhook，TUI 只复用公共契约。
7. **单列宿主与上线出口。** Git/LFS 专用 `/-/git/<repo-id>` 写入口和 read_url/push_url；同域 hosting 的 CSP sandbox/preview/deploy/readback/rollback；真实 sshd、bubblewrap、SMTP/TLS、浏览器和物理控制台。存量迁移与备份恢复先演练，不能以新安装通过推断生产安全迁移。

每个 feature 遵循失败测试→实现→重构，并补齐确定默认值、样例或 empty/disabled/deny、doctor、自检与启用配置 CI。PostgreSQL 保存唯一业务事实，Valkey 只可选唤醒；不另建工作流引擎。提交、远端 CI、发布和部署分别记录，不复用历史结果。

本轮新优先级以 23:08 第 12、15 章为依据：DM 已有后续工作树核心切片但未完整验收，现有 communication.send 本身不构成替代。提交 f085e7f 的 CI 36278494686 已通过；本次只更新需求文档，不新增代码实现证据。

## Agent 原语的依赖次序（23:14 新增）

先完成当前 CA/DM/ReadCursor/presence/claim 增量验收，不把新增原语一批全塞入。随后先规范已有 receipt 和 watch：同幂等提交可验签、uncertain 如实表达、当前授权过滤、Event 去重，再补 subject/query 订阅和主体视图。presence/claim 已有核心切片和18项相关测试，先补 doctor/selftest、CLI/主体视图与最终回归；再交付 handoff/checkpoint/lease 的小切片；依赖稳定资源引用、签名、TTL、通知，但不依赖新工作流系统。request/offer 匹配只输出建议，最后在安全 patch/base_revision 底座齐备后实现 proposal accept 的正式操作。各切片先测试不转权/不自动执行与隐私，再补 CLI 和完整第15章默认/样例/doctor/selftest/CI。

所有 Agent 视图默认空；presence 默认 unknown/关闭，presence 实现选择默认300s、范围30–3600s，不能写成权威文档指定；lease 有限 TTL 的数值仍待确定。不要把现有 handoff 模板、EffectJob 租约或基础 watch 算作新 feature 已完成。

## 23:33 新增身份连续性优先级

当前未提交工作树（DM/ReadCursor/presence/claim/Git HTTP 小包/续签与 CA）本地220 tests、8 conformance、构建通过；先收尾本批、提交并单独验证CI，不把新恢复体系混成同一巨大变更。

下一轮优先顺序调整为：**双钥基础与迁移→短主体路径/等价别名→自托管 RecoveryEnvelope/Policy/Custodian→托管双钥升级与恢复→LegacyDirective**。双钥先确定独立 key_id、标准 age recipient、客户端生成/持钥证明、历史钥读取/轮换；明确旧主体缺加密子钥的迁移策略，不能静默复用或转换签名/SSH钥。路径层复用同一授权和handler，文档/CLI输出短规范名，旧长名永久等价。

恢复密文先做自托管显式 opt-in 和 age OR 多 recipient 的可验收闭环，平台配置只存公钥；不做门限恢复。再实现加密双钥 vault、server-signable/server-decryptable 披露、同 subject 恢复/旧密文rewrap/托管token撤销/旧私钥销毁审计。LegacyDirective 可先交付只记录意愿、本人签名且禁交互的资源切片；任何实际恢复/归档/公开/handoff动作须依赖前述当前授权与audit/receipt，不能被遗言直接触发。其他Agent原语与展示继续分批推进，不抢在双钥安全契约之前。

## 23:41 范围调整与下一轮出口

当前self-custody双钥/主体别名切片已通过后续228项本地全套，仍需本批CI门槛；之前220 tests/8 conformance只对应双钥前，不直接沿用。Recovery/Legacy继续按前述依赖推进。

读取线优先补Registry稳定纯路径查询语法，覆盖现有query-string筛选/排序/fields/limit/type/subject，要求同授权/缓存/cursor/错误；随后复用GET Path Transfer构造有限期签名QueryRef，验收超长查询分片、逐次鉴权、撤权与过期。不要先实现只能query-string的新读取能力。

治理线先实现TopicMembership/TopicBan、created_by不变、最后admin保护以及成功操作同事务Event/本人通知；再做_events.md虚拟投影。默认10条compact+版本化短码schema/base-time，normal/proof语义一致，SyncCursor增量完成后再报完整feature。治理权限、保留名、第三方/被移除者最小披露在首个切片验收，不等界面完成。Topic成员不是组织成员，也不是DM固定双主体的替代。新需求均未实现，分线交付而不扩大当前未提交批次。

## 23:58 优先级调整

当前工作树最新228 tests/8 conformance/build通过，双钥已纳入本地全套，仍未提交或通过本批CI；此前220/双钥尚待全套的描述属于历史节点。

**下一步先补RouteSpec与副作用GET安全边界，再新增Topic/QueryRef/Recovery功能。** 先枚举真实handler注册effect，保证普通路径只PURE_READ/LOCAL_EPHEMERAL；对成功/拒绝/不存在和编码/方法变体比较全部业务表与外部调用。再实现passive-client guard，覆盖拿到完整有效执行路径的crawler/preview/scanner/prefetch/普通浏览器仍不能执行；unknown/Agent有效proof正常、伪UA无proof失败。最后全站输出检查ready-to-execute URL泄露与no-store/noindex/nofollow。受控浏览器例外必须显式部署配置，不允许UA获得授权。完成定向矩阵与全套后，才把本批安全边界列为交付，后续功能沿此前分线依赖推进。
