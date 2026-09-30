# 发布验收与现场交接

本页是发布验收的入口，取代把历史进度数字当作当前部署结论的做法。
本轮基线为已合并 #186 的 `dfb89fe9`，tree `e1d65b62`；#185 及其前置修复已在历史。
本轮事项与未完成范围见仓库根目录 `PROGRESS.md`。
每次最终验证以产物 `source.json` 中的 commit/tree、对应 workflow 和完整 JUnit
node-ID 为准。PR 的模拟 merge commit 与分支 head 可不同，必须核对实际 tree。
本页索引测试，不单凭测试文件名声称原设计所有分支或生产已经验收。

## 必须同时通过的发布闸门

| 闸门 | 可复查证据 | 拒绝条件 |
| --- | --- | --- |
| Python quality | Ruff 0.16.9 JSON、空 stderr、format、Python 3.15 compile、工具链/源码记录 | 任一诊断、warning、格式/编译失败 |
| Rewrite contracts | 八分片 manifest/JUnit、完整 node-ID 互斥并集、conformance、实际 tokenizer、wheel/sdist | 失败、错误、跳过、重复、漏项或不一致源码 |
| Deployment rehearsal | 同一 wheel 的哈希锁 server/client 干净安装、真实启动/重启、四传输、doctor、worker、真实 sshd/Git、正式 selftest | 包代码/元数据不符、未测依赖、安装/执行/清理失败 |
| 适用专项 | Recovery、Client、Hosting、Tool、Storage、Credential、架构、协作与 Transfer 的当前运行 | 不用旧分支 success 替代当前结果 |

部署演练仅用可销毁 PostgreSQL、回环监听器、独立 Test Root 与测试 SSH keys。
server 包在 checkout 外运行，Root 目录确实不可由网络账号读取；重启后同请求
返回原结果。四传输读和 doctor 比对所有表行值与 sequence，不只比数量。
真实 sshd 不改管理员 SSH：保留 PAM 账户检查、StrictModes、公钥认证、禁 shell/
转发；验证多 ref push/clone，撤销同时作用于新连接和已有 ControlMaster 的新通道。
client-only 包不得安装或导入 server runtime；其传输检查使用 MockTransport，
不冒称真实远程服务器连接。安装包正式 selftest 的 Test Root 也不代表真实物理控制台。

通过结果与 ZIP/wheel/锁文件摘要附在对应 PR 和 #83，不为写入成功数字再改代码树。

安装包演练还执行固定四个独立签名客户端的 32 次写入与 32 次读取，输出实际
耗时/吞吐/p50/p95、daemon RSS/峰值 RSS、内容文件字节和剩余磁盘。真实 SIGKILL
后重启须保留所有表值、sequence 和同请求幂等；真实 PostgreSQL/Git/CAS 备份
恢复须逐表逐值比较，仅允许确切的 runtime_config 暂停及持久 quarantine 改动。
删除 marker 后重新创建应用仍须隔离业务与幂等重放。失败直接使部署演练失败。
这些数值仅代表该次有界 CI 工作负载，不是目标主机容量或负载上限，也不取代
共享卷、多实例、断电、旧生产数据及真实恢复 pin 的现场验收。

## 开放 issue 的源码与验收入口

下面是具体已有断言的定位，不是“文件存在即关闭”的判定；完整条款仍按各 issue
与权威设计审查。跨表/跨存储、当前授权、历史签署字节和只读零副作用的断言不能放宽。

| issue | 当前核对入口 | 仍独立需要的证据 |
| --- | --- | --- |
| #64 | `test_url_secrets`、`test_request_target_safety`、真实 `test_nginx_secret_logs` | 实际所有监听器/CDN/代理/APM/日志链及部署配置来源 |
| #65 | 固定旧源码 fixture、`test_legacy_ledger_snapshot`、`test_ledger_migration_references/json` | 真实存量快照的合法来源、保护摘要、冻结时间与该快照迁移/回滚 |
| #68 | `test_custodial_lifecycle/inventory_ack/historical_migration`、`test_custody_exit_evidence`、`test_backup_retirement_attestation` | 真实 retained 密文逐项解密；独立备份范围退役，不推导自动销毁 |
| #69 | `test_complete_recovery_proof`、`test_recovery_authority_reconcile/current_authority/runtime_generation` | rollback 集之外的当前可信 pin、真实恢复状态及受控本机 promotion |
| #70 | `test_online_ca_policy`、`test_root_rotation/resume`、`test_task_a_root_files`、`test_money_admin` | 真实 VT/串口控制台、有限 CA 清单与明确重签/撤销批准 |
| #72 | `test_market_71/72`、`test_bounty`、`test_market_explicit_acceptance/recovery/notification_reuse`、正式 `market_e2e` | 新/旧政策分别核验，不将自动 settled 写成买家已 claimed/accepted；无生产资金操作 |
| #76 | `test_authorization_sources/matrix/recovery_boundaries`、`test_share_transport_matrix`、`test_read_projection_cache_transports` | 全条款逐来源/表示审查；现场启用方式归 #84 |
| #77 | `test_personal_content_signatures`、`test_notes_todos`、`test_legacy_directive`、`test_achievements/achievement_pins/honor_diagnostics` | 全保留历史和当前失效事实；荣誉不授权限、不宣称非人认证 |
| #78 | `test_read_entry_contract_matrix`、`test_read_query_keyset_boundary`、`test_search_source_relation_filters`、`issue_78_80`、cursor/Sync 回归 | 完整查询/输出/成本矩阵；不将概念名称偷偷作为新 wire 别名 |
| #79 | `test_file_operations/copy_integrity`、`test_text_patch_rebase_batch`、`test_patch_rebase_identity`、`test_personal_content_signatures`、`issue_78_80` | 完整文件/patch/Revision 矩阵；DB 原子提交不冒称物理文件回滚 |
| #80 | `test_transfer_status_cursors`、真实 `test_git_http_push/git_lfs`、`test_storage_commit_faults/lfs_shared_gc_quota`、真实 sshd 演练 | 目标共享卷/多实例拓扑的容量、断电、GC 与一致恢复 |
| #81 | `test_smtp_socket_acceptance`、`test_webhook_*`、`test_effect_completion_fencing`、`test_collaboration_*`、`test_lease_list_bounds/deadline` | 受控外发接收端及实际部署开关；lease 不授权或充当排他锁 |
| #82 | CLI/TUI/stdio 回归、`test_tool_sandbox_acceptance`、真实 sshd 演练、client-only 安装 | 目标机实际路径/PAM/systemd/隔离；TUI 只读导航，正式写入走签名 CLI |
| #83 | `DESIGN_CONTRACT_INVENTORY`、`test_configuration_field_contracts`、`test_manifest_feature_linkage`、Registry/route 矩阵、正式 selftest | 导航/正则匹配不等于逐条断言；现场闸门保留 |
| #84 | 本页三层闸门、`test_capacity_bounds`、`test_restore_physical_failure`、只读 preflight | 真实主机、旧数据、Root、日志、容量、备份/切流与回滚批准 |
| #85 | `test_market_boundaries`、`test_architecture_ports`、`test_client_boundary`、session/storage 边界、严格 Ruff | 保留唯一 owner 和兼容导出；不以删断言/unsafe fix/skip 换通过 |

旧 issue 中“#103 未合并”“完整 recovery promotion 未实现”“工具没有 concurrency
闸门”“没有 receipts 路径”等记录只归属其当时基线，不能覆盖当前已合并源码。
新默认显式验收与旧自动结算分别通过版本/policy 保持，不重写历史 receipt。

## 现场放行所需材料

1. 目标主机与只读运维入口、准确的部署版本/解释器/配置摘要、服务与数据库角色、
   所有 listener/vhost/upstream/日志和 APM 链清单。公开 health 200 不能证明新版本就绪。
2. 真实旧安装的受保护备份及 manifest、来源/冻结时间、旧 Root/CA 公开清单和
   已批准的新 Root 导入/身份映射。不要将备份、PIN、私钥或凭据贴到公开 issue。
3. 授权操作者在真实 VT/串口完成 Root/CA 审核；独立 current pin 和实际备份
   退役证明。SSH、TTY 伪装、数据库布尔值或删除 quarantine 文件均不能代替。
4. 目标拓扑的隔离容量/崩溃/恢复演练、适用 SMTP/Webhook 的受控接收端；未启用
   渠道仍验证零外发。提供切流/停止/回滚条件；产生新写入后不得直接退回旧 SQLite。

`python -m msg.admin.preflight --config /explicit/target/config` 仅执行读事务，输出
来源、账本/CA 与缺失证据。其 `decision=blocked`/退出 2 是明确未放行，不是可忽略错误。
没有实际材料时保留对应 issue 开放；CI 全绿仅允许交付这个准确源码树的发布包。
