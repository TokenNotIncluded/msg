# Task A：身份、Root 与隔离恢复增量

Refs #64, #65, #68, #69, #70, #84, #85；接续 #112，不关闭现场验收项。

## 基线与协作

读取 main ref 得到 `67401759a853c69fc5cb234982e1f7020b650542`，而不是 PR 元数据中较旧的 base_sha。
本增量以 #112 已整合该 main 的 `512bb9b1e163151d66b94a5bc26f4b3cc6415070` 为基线；
未向 #112 或 C 的分支推送。C 是唯一串行合并负责人。

本组新增 `admin/preflight.py`、`admin/recovery_replay.py`、`security/root_files.py`
及 `tests/test_task_a_*`；窄改动为 `security/custodial_migration.py`、
`plugins/custodial_lifecycle.py`、`admin/rotation.py`。
唯一公共测试接线补丁是 `test_authorization_recovery_boundaries.py`，需由 C 审查采用。
没有修改公共 DDL、executor、application、worker、配置默认值、已发布 operation/schema/短码或现有 CI。

## #68：空清单也验证签名挑战

原 `snapshot()` 在没有 ACK 时不会验证存储的服务端挑战签名，修改 subject/challenge 或四个新旧 key ID
仍可能报告历史已恢复。新增回归先得到 **9 failed / 1 passed**，再增加挑战的完整 Ed25519 验证、
同账号旧 IdentityKey/EncryptionSubkey 绑定及正在迁移的新主 EncryptionSubkey 未退役检查。
旧签名凭据只作历史验证，不恢复当前权限。没有更改旧正文、签名、Revision 或 key_id。

空历史 v1 路径的身份切换必须使用数据库中的真实阶段，而不是假设已进入 `pending_rewrap`；
该兼容修复保留 #100 的身份切换、A 类重加密、B 类本账号旧 age 子钥恢复封装和客户端真实解密 ACK。
在线清除、身份切换、历史恢复和备份退役依然分别报告。没有独立备份材料时
`backup_retired` 和 `server_key_retired` 不变为 true。

## #69：独立精确检查点下的 deny-only 重放

`TrustedCheckpointPin` 必须由受信任的本机调用方通过独立认证渠道取得：服务、公开验签钥、
完整检查点摘要、单调序号四项均必填。不能从待恢复数据库、其备份、可一起回滚的审计链或待验证 packet
提取这些值然后声称建立了信任。模块没有默认信任钥、环境变量、网络操作或配置解除隔离开关。

检查点语法在 `src/msg/data/recovery-checkpoint.schema.json`。每项事实恰有
`sequence/kind/subject/target/at`；额外字段、未知 kind、错签名、错服务、错备份摘要、序号回退、
缺口、目标不存在和跨账号目标都拒绝。`sequence=0, entries=[]` 仅在签名正确、独立 pin 精确匹配时
表示“这个已明确核验的空日志”；缺 pin 不等于空日志，也不允许 promotion。
同序号内容变化会被精确 pin 拒绝；跨水位的已重放前缀不一致会拒绝。

支持 credential、certificate、ShareGrant v1/v2、ShareLink 撤销，普通 Membership 与 TopicMembership 移除，
IdentityKey/EncryptionSubkey 退役，以及指定账号在线 vault 销毁。 `topic_ban.apply` 同时将旧快照里的话题成员降为 member/removed，与正常禁言一致；后续 `topic_ban.lift` 不恢复旧成员资格或 admin 角色。只收缩现存权限：不新增身份、授权、
密钥、财务交易、外发 job 或任何历史正文；证书的原始签署 body 不改。
`g_public` 是虚拟成员关系，不能通过修改一条 Membership 假装移除，明确拒绝该事实。

重放复用现有 PostgreSQL 写事务和 advisory lock。所有撤销、检查点收据、授权 epoch 和审计一起提交。
每次都重新应用完整日志；相同数据库收据不允许跳过已经从旧备份复活的权限。
并发、进程重入和真实数据库 backend 被终止后的事务回滚/重试都有回归。
公开本机 `replay()` 先调用现有实际 OS 控制台检查；`_replay` 与 `_provision` 一样只是内部用例，
不注册为 HTTP、SSH、MCP 或公共 CLI 操作。

**边界：这不是 #69 完成或生产提升。** 现有模块仅重放列出的 deny-only 事实。
完整 ACL/TopicBan/授权策略快照协调、独立日志的现场来源与当前性证明、完整不变量验收和受控 promotion
仍未完成。数据库收据只是一致性防线，不是抗数据库整体回滚的信任根。
隔离标记始终保留，`promotion=blocked`、`backup_retired=false`；没有把 quarantine 当作“已重放全部当前事实”。

本机诊断（不读取安装、不连接数据库）：

```sh
python -m msg.admin.recovery_replay --selftest
```

输出明确限定为 `isolated_signature_contract_only`；它不替代 PostgreSQL、真实控制台或生产证据。
包内 `recovery-checkpoint.example.json` 只包含示例公开钥和签名，没有私钥，
绑定 `https://isolated.invalid` 和全零备份摘要，不能用来提升真实恢复实例。

## #112：外部隔离与内部授权证据分开

旧集成测试在已隔离恢复后期望 `permission_denied` 和 mailbox/sync 成功投影，实际先在
`/_transports` 得到 `recovery_quarantined`。该回归先真实失败，再作以下分离，未提供生产旁路：

- 备份前，用真实签名和四种客户端保留全部精确 `permission_denied` 断言及 inbox/outbox/sync 字段裁剪断言；输入消息必须非空。
- 真实 PostgreSQL/Git/CAS 恢复后，精确比较资源、grant、消息、事件、成员和凭据投影输入，不重写签名历史。
- 在恢复后的只读事务中直接验证既有低层 `share_source_active`：撤销源为 false，独立未撤销源为 true。
- 外部客户端、业务目录、历史及 mailbox/sync 一律精确拒绝；worker 不能领取。隔离没有被移除或临时禁用。

此补丁由 C 串行整合，不修改 authorizer 实现或为它添加 allow-quarantine 参数。

## #70：Root 文件和并发 journal

`rotation_lock` 在实际受保护 Root 目录用描述符检查后取非阻塞排他锁，覆盖准备和完成。
并发准备至多一个 journal 成功，另一方明确 busy/pending；不覆盖已经待确认的 journal。
`read_private` 拒绝符号链接、硬链接、非普通文件、不属于当前操作者的文件和非 0600 权限，
并检查读取过程中 inode 内容大小/时间的一致性。目录必须为 0700。

完成阶段在改变数据库 Root 前检查历史旧 envelope、当前 envelope 及 old/new trust 一致性。
文件复制和实际路径继续沿 #112；旧 v1 journal 不被当作 v2 批准。
数据库已提交但文件尚未完成时仍可重入，不把新钥误存成旧历史，不重复审计。
这些是独立 Test Root 的源代码测试，不是真实物理控制台验收。

不修改旧 CA 的 grants，也不因升级新增 operation 自动扩权。
Bank fund 的“授角色”和“注资”属于 B 的两项确认；A 已只读审查 #103 的
`_confirmed_money` 和 `MoneyAdmin.execute`，后者在载入安装前执行实际控制台门槛，
两项摘要绑定确认均在 PIN 解锁前。组合集成和真实控制台证据仍由 B/C 与现场操作者共同完成。

## #64/#65/#84：只读 preflight，不替代现场证据

```sh
python -m msg.admin.preflight --config /explicit/authorized/copied-config
```

参数必须显式给出。命令使用 PostgreSQL `REPEATABLE READ, READ ONLY`，不实例化会初始化 schema 的
Application/MetadataStore，不做 DDL、迁移、计数器推进、目录修复、Root 解锁或外发。
检查复用既有 LedgerAccount 引用分类与守恒逻辑，以有界游标摘要历史账项原字段；
不把历史 receipt 中合法的 escrow account ID 当作用户身份，也不放宽未知权威引用/未知表拒绝。
空数据库明确报告 `empty`，不创建表。缺配置/数据库、材料不一致和缺现场证据分别保持 blocked。
命令输出 JSON；blocked 的进程退出码为 **2**，不是绿色放行。

该报告永远区分代码一致性与现场证据。以下材料本轮均未取得：实际部署版本/配置、全链路日志/APM、
受保护的真实旧库快照及来源、真实数据迁移/回滚守恒、独立当前撤销检查点、物理本机控制台、
存量 CA 逐项重签/撤销批准、B 的 bank fund 现场边界、实际容量/崩溃恢复测量。
因此 #64/#65/#70/#84 现场项继续 open/blocked，不以 fixture、SMTP sink 或 CI 成功补齐。

#103 `money_purchases.escrow_account` 为 NOT NULL 且指向 LedgerAccount；`purchase_escrow` 不是 Subject。
其 market DDL 在同一初始化事务内先于 `_migrate_ledger_accounts`，并保留未知 body 表拒绝。
`store_order_events.actor` 属身份引用，receipt_refs 为历史财务引用。
该只读复核不代表 #103 与本增量已组合验收；最终合并后仍须运行真实旧代码迁移/回滚用例。

## 验证记录与交付条件

本地隔离环境：Python 3.13.5、PostgreSQL 17.11、age 1.2.1、Git、Valkey 8.1.1。
它不是项目最终目标 Python 3.15/PostgreSQL 16/Valkey 9 的替代品。

初始失败证据：空历史绑定 9 failed/1 passed；Root 文件/并发问题失败；
恢复授权交集 1 failed（精确 `recovery_quarantined`）；新增 checkpoint/preflight 模块在实施前无法导入。
实施后：空历史/检查点/preflight 独立组 29 passed；Root 新旧回归 12 passed；
既有 v1 与完整托管生命周期兼容组 14 passed；恢复交集先通过 1 项，后续另加未撤销源阳性对照。
这些分组有重叠，不能相加冒充完整测试总数。

最终远端 head 必须另外完成现有完整四分片、精确 JUnit node-ID gate、conformance 及 wheel/sdist。
最终 source SHA、CI run 与实际数量在 PR/issue 的验收评论中记录；未得到最终结果前不声明成功。
没有部署、生产迁移/恢复、生产 Root/PIN/私钥访问、真实资金或真实邮件/Webhook 外发。

## Typed policy ceiling checkpoint v2 (partial, not promotion)

`msg-revocation-checkpoint-v2` preserves the v1 contract and uses a separate
`recovery-checkpoint-v2` signature purpose. Its exact independently supplied pin
is still required. It adds typed `value` payloads:

- `resource.acl.restrict`: `owner`, `group`, `mode`. Owner and group must match
  the restored resource; the mode becomes its intersection with the supplied mask.
  Repeated or broader masks never restore removed permission bits. Current resource
  generation advances when changed; historical Revision bodies remain untouched.
- `topic.policy.restrict`: `membership_policy`. Existing and supplied policies
  intersect: open accepts the supplied restriction, closed remains closed, and
  incompatible approval/invite ceilings become closed. It never reopens a topic.
- `topic_ban.set`: explicit `expires_at` (UTC or null), bound by the signed fact.
  The target must be a topic; the existing ban/member-removal replay path is used.
  Active bans combine conservatively: permanent dominates finite; otherwise the
  later expiry wins. A shorter expiry never lifts a previously active ban; only
  an explicit `topic_ban.lift` fact lifts it.

The packet must state `coverage.complete=false` and an exact sorted list of the
fact domains it contains (`resource_acl`, `topic_policy`, `topic_ban`,
`revocations`, `certificates`, `resource_authority`). Missing/mismatched coverage or a claim of complete recovery is
rejected. The packaged schema is `recovery-policy-checkpoint.schema.json`.
All facts, receipt, authorization epoch and audit commit together. Existing v1
prefixes can continue into v2; rollback to v1 after a v2 receipt is refused.

This is conservative restriction, not reconstruction of all current authority.
Full derived-authority reconstruction after owner/group/parent changes, complete
external log provenance/currentness, exhaustive inventory and controlled promotion
remain unfinished. Certificates are still revoked through existing facts; their
signed body is never rewritten to manufacture current grants. Neither coverage
metadata nor an isolated test establishes external freshness or backup destruction.

### Current certificate fingerprint and staged ownership/parent reconciliation

Two additional typed v2 facts advance reconciliation while `complete=false` and
persistent quarantine remain mandatory:

- `certificate.current` binds a subject/certificate ID to `value.body_digest`,
  the canonical digest of its entire currently expected signed Certificate, or
  null if that certificate has no current authority. A restored body that differs
  is revoked as a whole; matching already-revoked certificates remain revoked.
  Grants, constraints, issuance bounds, validity, key and chain/source bindings
  are all covered by the digest. The original signed body is preserved, never
  rewritten to invent narrower grants. Missing facts do not mean a complete
  certificate inventory or authorize untouched certificates.
- `resource.authority.reconcile` carries exact `previous` and `current`
  `{owner,group,parent}` maps. The subject binds the previous owner. Current owner
  and group must exist; parent must exist and remain acyclic. The actual snapshot
  must match the previous or already-applied current state. A contiguous sequence
  for one resource is verified and collapsed to its final state, allowing old,
  intermediate and already-applied snapshots to converge without transiently
  restoring older ownership. Interleaved ACL/policy facts must bind the exact
  owner/group state at their signed sequence position; replay applies only their
  restrictions to the actual old/intermediate/final resource. Unrelated or
  temporally mismatched bindings, gaps or unexpected states abort the replay.

Ownership/parent changes stage metadata only inside the quarantined instance;
resource mode (including CERTGATE/SETGID/STICKY) is preserved and historical
Revision bytes remain unchanged. The receipt names `authority_rebuild_required`
resources and `derived_authority_inventory_incomplete`. Existing certificates
and new/old scope-derived permissions are not declared valid or rebuilt by this
operation: ordinary reads/writes and effect workers remain blocked before and
after it. There is no promotion path. A full current authority inventory and its
independently current provenance are still required before any future promotion
implementation could safely release these staged changes.
