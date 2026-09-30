# 交付进度 · 2026-09-30

目标：逐项处理全部开放 issue/PR，交付经验证的安装包和部署步骤。
进度以实际提交、测试运行和现场材料为准，不把旧记录或测试数量当完成率。

## 本轮基线与完成事项

- 已核对 16 个开放 issue（#64、#65、#68、#69、#70、#72、#76–#85）；
  #186、#187、#188 已审查并按 expected head 依次合并。
- 已读取当前权威设计及有效归档；现有功能和历史协议保持原义。
- 当前基线 main `c8ed2610ab774dba3085972683c1d2c8a9232222`，
  tree `89f8e4119e625cb421b75be468392b2d7b187930` 与 #188 实际验收树一致。
- [核心验收 36644965337](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36644965337)：
  八份 ZIP 摘要、manifest 和 JUnit 真实 node-ID 已核验，2569 core + 8 conformance，
  无失败、错误、跳过、重复或遗漏。
- [质量 36644965239](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36644965239)：
  Ruff JSON 零诊断、stderr 为空、588 文件格式、Python 3.15.0rc2 编译通过。
- [部署演练 36644965142](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36644965142)：
  同一 wheel 的锁定 server/client 安装、真实启动/重启、四传输、只读 doctor、
  禁外发 worker、真实 OpenSSH/Git 撤权及正式隔离 selftest 42 项均通过。
  wheel/sdist 与核心构建产物逐字节一致；全部十份 ZIP、摘要与测量精确证据见
  [PR #188](https://github.com/TokenNotIncluded/msg.lmm.best/pull/188#issuecomment-5906443148)。
  合并后独立主线部署/质量/恢复专项已 success，发布四文件与已验 #188 逐字节一致；
  主线 Rewrite 完整门槛仍排队，不能提前算通过。

## 当前推进

- 已完成安装包的有界并发写入/读取测量、真实 SIGKILL 重启与全表/sequence 不变量检查。
- 已完成安装包的真实 PostgreSQL/Git/CAS 备份恢复计时和全表值比较；只允许恢复流程
  明确规定的 runtime_config/quarantine 变化，验证删 marker 后仍隔离业务与幂等重放。
- 首次新演练 [36643217304](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36643217304)
  在真实恢复时失败：私有 Git 打包引用后，ZIP 丢失空 refs 目录，Git 无法识别仓库。
  本轮修复私有/用户仓库统一结构目录重建，保持只读 proof 校验不修改磁盘，
  并新增 packed-ref 恢复、只读拒绝和符号链接拒绝回归；#187 修复后的完整验收已通过。
- #188 新增真实 COMMIT 故障回滚、主体奖励限额/版本拒绝，以及 R1–R5 各轮
  nonce/question/单轮 TTL/答案、跨主体与并发授勋回归，均已在完整门槛执行。
- 当前补核对 #72：六种公开搜索/真实 CLI/历史签署内容状态、CLI PoP 真实重启重试；
  `docs/BOUNTY_ACCEPTANCE.md` 给出原清单逐断言映射，新回归须云端实际通过后收口。
- 逐条核对 issue 中尚未刷新到当前源码的描述，保留原验收范围和历史证据。

## issue 交接

| issue | 已有代码/隔离证据入口 | 本轮需要继续完成的范围 |
| --- | --- | --- |
| #64 | URL/Host/真实隔离 Nginx 日志回归；#186 主体读 effect 栅栏 | 实际 listener/CDN/proxy/APM/日志链材料 |
| #65 | 固定旧源码 fixture、LedgerAccount 迁移/回滚 | 真实旧快照的合法来源、冻结点及快照演练 |
| #68 | 历史密文迁移、逐项 ACK、备份退役证明校验 | 真实保留密文及独立备份范围退役证据 |
| #69 | 完整 proof/current-policy reconcile/promotion、持久隔离 | rollback 集之外的真实当前 pin 与本机受控恢复 |
| #70 | Root/CA/控制台拒绝与 bank fund 源码回归 | 真实 VT/串口及有限存量 CA 审核 |
| #72 | Bounty/官方 market_e2e/邮件/恢复/版本政策回归 | 对照原清单逐断言验收，更新过时的 #103 描述 |
| #76 | 多来源授权及跨协议/恢复矩阵 | 完整投影、缓存、当前权限条款核对 |
| #77 | 私有内容、独立签名、Legacy、荣誉挑战与展示 | 全条款/客户端/当前失效事实核对 |
| #78 | ReadQuery/Search/Sync/游标及 #186 | 完整成本/表示/当前授权矩阵核对 |
| #79 | 文件、patch/rebase、Revision、原子发布/物理失败 | 完整内容/文件条款核对 |
| #80 | Transfer/Git/LFS/hosting/一致存储及真实 SSH | 共享卷/多实例目标拓扑容量与持久性 |
| #81 | 通知/EffectJob/协作/SMTP socket/Webhook 回归 | 全事件契约与受控部署接收端 |
| #82 | CLI/TUI/工具真实隔离/SSH/安装包 | 目标机路径/PAM/systemd 与全部客户端条款 |
| #83 | 配置、Registry、规则及有限设计分母 | 逐条断言映射、当前文档与真实现场闸门 |
| #84 | 锁定安装包、隔离服务/SSH/selftest 演练 | 本轮补测量；真实主机/旧数据/切流/回滚仍需材料 |
| #85 | 唯一市场 owner、受保护账本/版本兼容/严格质量 | 对当前热点/重复实现核对，不按旧目录诊断 |

## 完成条件与外部阻塞

新改动须在同一最终 tree 上通过质量、完整八分片/协议、安装包演练与适用专项；
合并后再核对独立 main push。问题解决后才更新状态，不能删断言或把取消当通过。

现场尚缺：目标主机只读入口与配置/日志链、受保护真实旧备份及来源、真实本机
Root/CA 操作者、独立当前检查点、密文/备份退役材料、目标容量/停止/回滚条件。
所需材料及操作步骤见 `docs/RELEASE_ACCEPTANCE.md` 和 `docs/DEPLOYMENT.md`。
当前没有生产部署、资金操作、真实外发、备份删除或恢复放行。
