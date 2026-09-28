# 项目设计差距报告

## #71–72：确定性清算与 Bounty（2026-09-28）

本分支保留 #71/#72 的确定性清算、银行边界、报价兑现与预托管 Bounty 实现，详细契约见 [MARKET_CLEARING.md](MARKET_CLEARING.md)。

| Issue | 实现与测试入口 | 边界 |
| --- | --- | --- |
| #71 | 精确整数清算、具名账项/幂等 entry key、本机 bank fund 双确认、Registry-backed hosting entitlement、pending/settle/refund 与恢复测试 | 默认零发行、空报价；购买资源不扩大授权 |
| #72 | 当前钥/nonce/TTL PoP、预托管预算、Claim+奖励+Event+Inbox 同事务、暂停/限额/关闭恢复，以及官方隔离 market_e2e | PoP 只证明当前签名钥控制，不代表真人或抗女巫 |

#73 的旧订单/交付实现不再覆盖新版代码；本分支已经吸收 #102 的 #73–75 实现，避免把当前订单、交付和仲裁语义回退到旧版本。

## #73–75：版本化市场实现（2026-09-28）

订单快照、自动交付、独立 checkout 邮箱验证和确定性仲裁已实现；接口、最终性、默认无仲裁员、旧 `@1` 兼容及运维边界集中见 [MARKET_CONTRACTS.md](MARKET_CONTRACTS.md)。下方 `135a190` 及更早测试数字属于历史提交，不能用于本批验收。

| Issue | 实现与测试入口 | 保留的部署边界 |
| --- | --- | --- |
| #73 | `market/orders.py`、`escrow.py`；不可变签名快照、显式状态表、单一资金释放、原子分账；`test_market_lifecycle.py` | 不改已签名 `orders.buy@1` 的含义；生产库迁移单独审查 |
| #74 | `market/delivery.py`、`targets.py`、`email.py`；managed 自动结算但不 claimed，sealed/service 显式验收，真实 Transfer 和买家 endpoint 复核；`test_market_delivery.py` | SMTP 只发最小通知；本地 fake sender 不代表真实 SMTP 验收 |
| #75 | `market/policy.py`、`arbitration.py`；固定候选/epoch/panel/quorum、一次申诉、私有证据、有效签名 Decision 由 escrow 原子消费；`test_market_arbitration.py` | 默认候选为空；撤权/缺 quorum 持款，不临时挑人或让 AI 自由裁量 |

`market_lifecycle` 隔离 selftest 覆盖 20 MSG 注资、10 MSG PoP 奖励、5 MSG 固定 bundle 自动交易及签名仲裁退款；只读 doctor 检查市场不变量。`test_market_recovery.py` 验证真实备份恢复、CAS 证据及恢复演练副作用隔离。验证结果以本批 PR 的具体 head 和 CI 为准，不能把这些模块扩写为整个项目或生产交易已验收。

## 历史：135a190 时点的工作树进度与验收边界

本次重新读取[ChatGPT权威设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，当前191个非空段；[已完成部分](https://docs.google.com/document/d/1FtTdF5uhBPAsi-so-jOfpsiVI19RWKgx6bzIEFvpR2E/edit)当前71个非空段，含A19/A20等归档条款。历史段数和旧CI仅作沿革；归档不取消未迁出需求及回归要求。

本轮代码已提交并推送至main：`135a190`。本地最新完整验证为541 core passed、8 conformance passed、隔离 build成功；PG LedgerAccount及引用保护定向9项通过。旧发行 `@1` 的三种操作由executor在幂等记录读取前拒绝；新增测试4项，加上恢复/batch相关定向测试，凭据释放与隔离相关共17项通过。`batch` 已禁止 `identity.token_recover` 和 `identity.custodial_create` 作为子操作；`identity.temporary@3`签名钥受限写入定向6项通过。提交对应 [GitHub CI 36318026014](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36318026014) 已 completed/success，日志核实541 core、8 conformance及build成功；生产未部署。

旧版 `identity.token_recover@1` 本身不是发现的问题；它原有严格的一次领取语义，问题在 batch 包装绕过隔离，现已加入子操作拒绝。设计全量验收和生产迁移仍未完成；本地与CI状态分别见上。

| 本批切片 | 当前工作树事实 | 尚待完成 |
| --- | --- | --- |
| LedgerAccount与旧escrow迁移 | 代码已随135a190提交推送；PG LedgerAccount及引用保护定向9项通过 | 未生产部署；真实存量库迁移、自动市场闭环与跨模块备份/恢复仍缺 |
| 凭据发行与恢复 | 旧发行@1三种操作在幂等读取前拒绝；新测4项，恢复/batch相关定向合计17项通过；batch拒绝token_recover/custodial_create子操作 | 代理日志全链路证据与托管钥退役验收仍缺 |
| 临时身份 | temporary@3签名钥受限写入定向6项通过；旧临时主体迁移纳入541项本地验证 | 生产存量主体升级与部署验收仍缺 |

135a190已提交推送且对应 [GitHub CI 36318026014](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36318026014) 成功；生产未部署，不得据此声称完成设计文档全量验收。
### 安全与语义边界

资金按整数minor_units入不可变PG账本，当前写事务串行化保护余额/总量/并发，签名收据有policy_version/digest/sequence。不能以此宣称完整ClearingPolicy/恢复矩阵完成。本机重复手动执行使用新的request_id，不能套用同ID网络重试的防重复承诺。

135a190中的 Order/Bounty escrow 已改用无主体的 system LedgerAccount，不再以 system Subject 承载；PG旧库中既有 escrow Subject 的迁移代码已提交，PG定向9项通过；尚无真实存量库迁移或生产执行证据。普通 money.transfer 仍拒绝 system 账户；账户分型及注册/恢复/委托/银行管理不能激活托管账户的完整负例仍待验收。

Bounty单项store.listing_get的当前state/pause_reason/budget已与bounty.get一致；历史Revision保留原值并附current_*。搜索与其他投影仍待全面验证。奖励支付复核当前bounty事实，未来缩小写锁需重测最后一份预算和主体限额。

资金现在不再只有funded入口：funded可取消退款，支持范围内的买家显式prepare/get/accept可结算。但这些不是自动Delivery；无Email endpoint绑定、四方owner/撤销复核、邮箱pending验证/最小邮件/secret禁明文SMTP完整流程。只读交付不自动claimed，accept由买家明确签名，不能把HTTP成功或SMTP接受等同收货。

`test_market_manual_flow.py` 已串起Test Root内部mint/银行登记/注资→10MSG预托管PoP奖励→5MSG固定bundle购买→买家显式prepare/get/accept→bank15/buyer5、total_supply20。它使用内部本机用例和手动交付，**不证明官方market_e2e要求的bank fund命令、自动Delivery、SMTP sink/未验证邮箱边界及全部失败矩阵**。DisputeResolver/仲裁和大型Transfer也仍缺。

下一出口：LedgerAccount分型与旧escrow迁移代码已随135a190推送，PG定向9项通过；仍需明确真实存量库迁移与生产状态。接下来补跨模块备份/恢复、自动交付与状态故障恢复、大型Transfer，再按买家身份绑定接Email并以本地sink验秘密/字段裁剪，最后跑官方market_e2e和仲裁独立矩阵。任何中间成功不自动授权生产交易或解除未兑现报价的拒绝。

## 历史：文档迁移与此前验证归属

已实时读取[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)迁移后修订 `ANLCKQmFOHtA75…`，196个非空段；以本节和当前正文为准，以下08:51等记录属于修订沿革，不再是最新基线。

用户要求的瘦身已由主执行者完成：迁移前201非空段、迁移后196；[已完成部分](https://docs.google.com/document/d/1FtTdF5uhBPAsi-so-jOfpsiVI19RWKgx6bzIEFvpR2E/edit)保存Todo、handoff、lease、presence、claim五个整段，及LinkSet入口、GraphQL读写分离、短码不复用、atomic batch四句。已读回归档内容；主执行者报告逐段期望变换完全匹配。归档依据e126539与[成功CI36306888836](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36306888836)，仅表示这些条款完成，未迁出的条件和所有回归要求仍有效。

此前一批协作路径/CLI、货币基础、sale Listing/固定文件寄售包的完整本地结果为 **432 core passed、8 conformance passed、uv build成功**；这发生在Listing mode小修之前。其后显式mode=sale、旧记录按sale兼容、bounty返回listing_mode_unsupported，store定向5 passed、综合定向40 passed；未声称修后全套再次通过。9/38/16定向组已包含在全套，不累加。e126539的423/8/build及成功CI归旧提交，不能当作432项工作树CI；没有本批生产部署或market_e2e完整闭环证据。

### 新规范已生效、实现仍需补齐

DCR-01/02/03主要建议已写入权威正文并进一步扩充，详见[变更记录](DESIGN_CHANGE_REQUESTS.md)，不再统称待批准。

1. 所有等价可重放秘密禁止URL；私有纯路径proof需绑定查询/主体/ID/expiry且重验授权。机密发行无安全通道返回secure_channel_required；接收者绑定密文、敏感查询保密、遗留服务端token URL分支与全入口/日志拒绝矩阵尚需验收。
2. 一次释放先原子消费资格、再发送响应；默认15m从业务提交起算，配置有界且重试不续期。@2/journal已有切片；旧发行@1三种操作现由executor在幂等读取前拒绝，`identity.token_recover@1`本身保留严格的一次领取语义，batch包装隔离已修。可配置窗口、恢复再次丢失、原谱系撤销/不扩权全矩阵不能仅凭现有测试称完成。
3. 历史A类rewrap副本与B类仅退役EncryptionSubkey的RecoveryEnvelope均须明确边界；冻结全保留历史、并发增量、新写入新钥、实际逐项解密ACK、identity_switched/history_recoverable/server_key_retired及online_retired/backup_retired、旧备份防复活仍未完整实现。旧vault仍可解密时不能宣称完整退役。

Bounty、完整清算/托管、Order/Delivery/仲裁和官方market_e2e仍未完成；此前432项本地结果仅覆盖当时切片，不把sale基础或归档条款扩写为整章完成。

## 08:51 需求新增记录（历史；当前进度见文首）

权威基线更新为 `2026-09-27T08:51:45.979Z`（198段，01–15章）。当时未提交的Listing/寄售包只对应sale基础，不能视为Listing mode=sale|bounty完整支持。货币相关38、store相关16、协作9项定向是局部证据，core全套已432 passed、conformance8 passed、build通过；e126539的CI36306888836已success（仅覆盖旧提交），不冒称本批完整通过。

- **Bounty**：publisher在激活前将budget原子转入BountyEscrow；它是无私钥系统LedgerAccount。固定reward/claim_limit（默认1）/max_claims/eligibility/verifier版本，预算不足自动paused/out_of_budget，可显式top-up；publisher离线仍发奖，不依赖事后手动付款。
- **signature_pop_v1**：服务端随机challenge绑定listing、claimant、nonce、issued/expiry与verifier版本，当前IdentityKey签名；校验签名/TTL/nonce单次消费/状态/eligibility/次数。只证明控制该主体当前签名钥，不证明真人/唯一自然人或反女巫身份。BountyClaim成功与奖励转账、receipt/Event/通知同事务，最后一份预算并发只成功一次，不得先记paid再转账。
- **checkout邮箱**：新未验证地址最多收到不含订单号、用户名、商品或提货能力的验证消息；站内交付照常完成，Email pending，验证后才能绑定。已验证且属于buyer的地址方可直接作为目标；worker继续复核四方主体/撤销/订单状态。邮件关闭或失败不阻止订单；不得把现有email验证功能当作该订单绑定流程已经完成。
- **market_e2e固定出口**：隔离Test Root mint 20 MSG并bank fund 20；bank预托管10奖励，buyer完成PoP得10，余额bank=10/buyer=10/bounty escrow=0；再买5 MSG固定bundle（指定文本与hello.txt内容），完成buyer→OrderEscrow→bank及自动Delivery、settled、摘要核对，最终buyer=5/bank=15/全部escrow=0/总量20。重复、过期、错主体/错key、重放、最后一份预算竞争均须拒绝且不重复扣款。SMTP sink/FakeNotificationSender验证最小邮件，默认不发真实外部地址；真实SMTP另验。

上述预算账户、挑战状态、Claim与订单/交付都必须作为PG权威事实一致备份恢复。当前没有该官方fixture完整闭环通过证据；普通签名transfer、sale寄售包或旧成就挑战均不能替代它。

**历史状态：`e126539981508cf25cda6ab9bbc9b3531449d203` 已提交推送，包含此前423 core / 8 conformance / build通过的协作底座、ReadQuery@2与logo；[CI 36306888836](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36306888836)已completed/success（仅覆盖e126539）。当时main工作树另有未提交增量：协作主体路径/CLI联合定向9 passed；货币基础相关定向38 passed、/store Listing/寄售包相关定向16 passed；当前core432、conformance8、build通过；尚无432项工作树CI或部署证据。**

## 历史快照：此前修订与下一批进度

已实时重读权威设计 `2026-09-27T08:51:45.979Z`（198段，01–15章）。本轮进一步明确ClearingEngine为msgd内置确定性程序：不是subject，无账户/余额/私钥/主动交易或自由裁量；同输入、账本状态、ClearingPolicy版本必须同结果。货币receipt含policy_version/digest与ledger_sequence，纠错只能追加refund/reversal。EscrowAccount是系统LedgerAccount而非用户；EscrowEngine只按订单状态和有效决议执行，仲裁员不能写Ledger。

- **已推送基线**：e126539的423 core、8 conformance、build为此前本地结果；CI 36306888836已success，不借旧main CI证明本提交成功，也不覆盖当前增量。
- **协作入口增量**：`/@user/handoffs/`、`leases/`及单项只读视图、专用CLI已有工作树实现，联合定向9 passed。读路径复用已有Operation/授权，不能以该切片宣称全部协作原语、全部客户端/路径、feature默认/doctor/selftest或整章已完成；尚未提交，已纳入432项本地全套。
- **货币基础增量**：money.state/banks/balance/ledger/transfer与PG账本字段已有源码；可见policy_version/digest/ledger_sequence等初步记录。相关定向38 passed，覆盖普通签名转账、不可变PG账本、零发行/余额/总量、并发双花/幂等、签名收据和root网络拒绝；不等于完整ClearingEngine或全部货币验收。本机msgd money发行/销毁/BankRole/报价、redeem/Entitlement与完整恢复矩阵仍缺。
- **/store sale基础增量**：sale Listing create/update/get与package_deposit/get已有工作树代码，相关定向16 passed，覆盖空/store、Listing修订/价格/条款、seller-only固定文件ConsignmentPackage与通用操作绕过拒绝。它们不是完整购买/结算/交付：Order、PaymentIntent/ClearingEngine完整契约、EscrowAccount/EscrowEngine、DeliveryTarget/Delivery、纠纷/仲裁及相关CLI/只读路径仍缺。

当时下一步是确认432/8/build结果的提交与CI归属；9/38/16定向组可能重叠，不累加为独立总数；货币按精确账本和确定性ClearingPolicy→本机中央银行/BankRole→报价兑换→Listing/不可变寄售包→Order/PaymentIntent/Escrow→买家交付/权限视图→客观退款和确定性仲裁推进。每步分别验证并发幂等、权限/失效、默认/doctor/selftest与一致备份，不把进行中的代码计作完成，也不自动启用生产交易。

### 08:51 权威需求与仍缺范围

本次按 Drive 返回的修改时间 `2026-09-27T08:51:45.979Z` 重新读取同一权威文档（198段，01–15章）。相对旧报告，货币与寄售市场是实质新增范围，不能沿用“无钱包/Store待定”来排除：基础身份/公开读/普通通信免费，货币不购买认证、CA、系统权限或优先级，禁止法币充值提现与收益承诺。

- **货币**：精确minor_units/scale=6、primary稳定ID、余额/双边账本/总量守恒；@root仅本机mint/burn/BankRole/转账/报价，银行不增发不透支；transfer/redeem、价格快照、Entitlement与pending/settle/refund。当时货币基础已有未提交38项相关定向通过；该批432 core/8 conformance/build已本地通过，其余契约仍缺。
- **寄售与订单**：/store Listing与不可变ConsignmentPackage；订单固定listing/package/价格/条款版本，随机不可枚举order_id且无权与不存在等价。managed_instant、sealed_manual与service交付不同；不是已有帖子/Transfer换个名字即可满足。
- **资金托管与仲裁**：buyer→Escrow→seller/refund/split原子记账；版本化客观故障处理，Arbitrator只签Decision不能改Ledger，确定性panel/quorum/利益冲突排除和限定appeal。不能借管理员或AI自由裁量补空白。
- **发货与隐私**：权威交付在买家订单/_delivery，Inbox只给最小引用，Email仅可选通道；下单锁定买家已验证endpoint，发货重新核对buyer/DeliveryTarget/邮箱owner/加密钥owner；seller不获真实邮箱，secret不明文SMTP，claimed不等于SMTP送达。
- **验收与持久化**：PG保存权威货币/订单/Escrow/交付/仲裁事实并一致备份；默认零发行量/余额、无银行/报价、空store与订单集合。双花/幂等/价格修订/授权裁剪/退款/交付错配/仲裁边界/恢复均须独立测试、doctor/selftest与CI。此前e126539的423 core、8 conformance与build只覆盖该提交已有功能，不覆盖当时未提交的货币/市场契约，也不证明整章完成或生产部署。

本报告记录实现事实与差距，不修改需求，不使用完成百分比。

## 历史快照：对照基线与证据

- 权威来源：Google Drive `ChatGPT` 文件夹（`1L0gl0AqThp100kRrviq-jorc04cPSnYO`）唯一[《msg.lmm.best｜项目设计》](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)。本次实时读取修订为 **2026-09-27T08:51:45.979Z**，01–15章。正文压缩不减少验收范围。
- 当前实现基线：e126539已提交推送，本地423 core/8 conformance/build通过；CI36306888836已success。下一批协作路径/CLI定向9 passed，货币基础相关38 passed、store基础相关16 passed；全套432 core/8 conformance/build已本地通过，均属于当时工作树、尚无该批CI或部署验证。
- CI：[36301644644](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36301644644)，已 **completed/success（408 core、8 conformance、build）**，对应完整提交 `52dd4b401800b4ecf0e42316ce7ad4f11ee1a679`。该CI只覆盖52dd4b4，不覆盖后续414项工作树。
- 已合并变更：content.text_patch@2显式rebase/text_patch_batch、ShareGrant@2 group/read-only空constraints/受限reshare已纳入414项本地验证；旧@1不变。PR #63已合并，使用main CI36302710481的成功结果，不借用52dd4b4的CI。
- 部署：没有本重写版本已完成生产部署、旧库迁移、生产在线备份/恢复或旧CA升级的证据。本机 `light.local`、真实git-lfs与隔离恢复均属于各自本地验证，不等于生产证明。

既有[实现状态](IMPLEMENTATION_STATUS.md)、[路线](ITERATION_PLAN.md)、[验收记录](VERIFICATION.md)包含逐批历史，一些早期“未实现/未提交”描述已落后；本报告以当前main工作树和明确归属的分层验收为准，不把历史测试相加。

## 状态用语

“已有切片”表示有实现与相应测试，不表示完整feature。“局部”表示目标的部分行为已有，仍缺协议、默认、测试或宿主证据。“未完成验收”不自动等于没有代码。第15章要求实现、默认值、样例或明确空/禁用/拒绝、测试、doctor、selftest、CI齐备；当前没有足够证据把任何整章宣布全部交付。

## 历史快照：逐章差距矩阵

| 章 | 状态 | 当前PR代码已有 | 剩余需求/验收 |
| --- | --- | --- | --- |
| 01 定位范围 | 基本符合，未整章验收 | 统一通信原语，无新增登录/通用工作流；运行限额 | 最新需求货币/BankRole/账本/Entitlement未覆盖；真实token/往返/失败重试持续测量、部署容量与恢复演练仍缺。 |
| 02 架构注册表 | 局部 | 模块化单体、Registry/OperationExecutor、版本化短码/多协议、可信插件 | 所有正式feature统一向量、完整插件迁移/停用任务矩阵；不能由旧CA通配符获得新增能力。 |
| 03 身份凭据 | 局部 | Ed25519+age/X25519双钥，托管AES-GCM vault，@2一次交付/显式恢复，客户端journal，受限双钥升级，RecoveryPolicy/Envelope，荣誉R1–R5 | 旧@1兼容仍非严格一次交付；历史密文迁移finalize仍fail-closed；外部密文可解性不可证明；完整恢复/托管解密生命周期、荣誉evaluator/pin/完整投影与全矩阵。 |
| 04 CA根管理 | 局部 | 三级CA/收缩/撤销检查、Basic Online白名单、独立Test Root、自检/只读authority_snapshot | 存量签名Root/Online CA新增能力治理、完整链/来源组合矩阵与真实控制台。改Registry不改旧签名；Root轮换会影响旧链。 |
| 05 资源/组/Topic | 局部 | Resource/Revision、mode特殊位；Topic四策略/角色/ban/虚拟事件；组织四策略/三角色、旧组织兼容、public虚拟组 | 所有授权来源组合、跨入口/失效/并发矩阵及完整feature映射；管理员不能据角色获取系统/CA权。 |
| 06 分享/个人空间 | 局部 | 直接叶资源ShareGrant及@2 group/read-only空constraints/受限reshare、默认off的POST-body ShareLink；Notes/Todo及本人到期提醒；SOUL/AGENTS；Legacy本人签名登记 | @2仅read且constraints必须空，不等于任意操作/约束转授；分享排除DM/SOUL/Todo/system/preview。完整私有内容签名/语义约束；Legacy只声明、不执行；恢复策略UI与完整生命周期。 |
| 07 内容讨论 | 局部 | 独立post/reply、模板/引用/归档、metadata/rollback、post_write/patch别名、LinkSet | text_patch@2显式rebase与text_patch_batch已有本地验证；仍缺完整操作族、unified/heading-block patch和历史generation映射。批量只承诺SQL引用原子发布，失败可留Git不可达孤儿，非跨存储回滚；客户端独立Revision签名仍不完备。 |
| 08 读取与发现 | 局部 | 短长别名、规则分片/wiki、history/diff/LinkSet、Page/ReadCursor、Sync与持久checkpoint、Read/Search QueryRef | ReadQuery@2已有children/replies一层独立分页；仍缺全协议/任意嵌套、全部字段/缓存/错误等价、token-only纯路径与长URL边界、完整HTML/TUI导航、Bookmark。checkpoint GET不推进，签名ack不是内容ACK。 |
| 09 操作边界 | 局部 | /-/分流、effect矩阵、passive GET guard、幂等与当前授权、稳定字典 | 全部真实路由/代理/方法/编码的零业务写入矩阵、部署日志与TLS。含恢复秘密的标准客户端请求禁PathGET；不能宣称任意路径客户端已经完成全部私有操作。 |
| 10 文件/搜索/Grep | 局部 | lexical_search@4：过滤、facet、source/relation、显式suggest；受限Grep、exact/context唯一patch | spell、全部搜索关系/来源/投影条件、完整file.*、unified/Markdown patch、更广rebase/batch、可选行指纹；全时序无泄露未证明。 |
| 11 存储/传输/托管 | 局部 | PG权威/Git/CAS/Transfer；真实git-lfs、Range、新LFS共用BlobStore、限定准入；同域禁JS、真实root Resource/private preview；backup v4 | 旧LFS迁移、全部署staging/其它CAS预算、多ref原子证据；preview需签名header非裸浏览器链接；完整浏览器矩阵；生产在线备份未演练，外部Git并发可能fail-closed。 |
| 12 事件/通知/协作 | 局部 | Inbox/DM/ACK、Sync、Todo提醒、presence/claim；新增handoff状态/CAS/最小通知和非排他lease TTL/续期/释放；Inbox Webhook及需webhook.domain的三类owner事件订阅 | 所有通知来源/偏好；真实公网/SMTP；handoff/lease路径/CLI在该历史快照中仍是未提交定向切片；完整Resource/Relation/Event/default/doctor/selftest契约；request/offer/proposal/工作checkpoint/watch完整契约。同步checkpoint不是工作checkpoint。 |
| 13 工具/客户端 | 局部 | CLI Search/Grep/hosting/recovery、MCP、受限SSH/工具、RSS；TUI只读Home/Inbox/Search/Thread | TUI全视图与交互；完整--json/--jq/--template等；真实sshd、bubblewrap、DNS/重定向/私网授权与生产运行矩阵。 |
| 14 配置与接口 | 局部 | msgd.toml、根/服务/缓存分离、源码规则按文件digest/version及显式迁移、requires_rules、恢复drill闸 | 每项配置完整doctor、精确RuleSet/全部规则覆盖、旧布局迁移、生产秘密备份恢复和操作流程。源码规则迁移不是自动改生产授权。 |
| 15 默认/TDD | 局部 | BootstrapManifest v5十二项feature rows、真实doctor/selftest映射、隔离Test Root；本批423项本地core测试 | 十二项不覆盖所有正式feature；partial/disabled只报告完成度、不关现有API。完整默认/样例/负例/故障/跨协议/CI矩阵仍缺，旧main CI不覆盖本批工作树，也不替代剩余feature验收。 |

## 历史批次：协作、嵌套读取与标识（core 423，通过范围有限）

需求修订：`2026-09-27T08:51:45.979Z`，ChatGPT 文件夹的权威项目设计。本批只更新实现事实，不修改需求。

- **handoff**：create/get/list/decide，pending→accepted/rejected/cancelled，发送者取消、接收者接受/拒绝，generation 条件更新与并发决策；最多16条资源引用，读取逐项按当前权限过滤。Inbox只通知交接ID/状态，不复制被引用正文或私有目标。幂等写回执只给摘要，不回显失效引用。
- **lease**：acquire/get/list/renew/release，TTL最多7天、generation条件更新，到期只读投影为expired，不在GET里改业务状态。不同主体可同时取得同目标lease；它不排他、不授写权、不替代数据库事务或base_revision。读取/续期继续验证目标权限；DM、系统规则、凭据、keystore等敏感引用拒绝。
- **协作剩余差距**：当前使用专用持久事实表和既有Operation/通知，尚未达到完整Resource/Relation/Event协作契约；`/@user/handoffs/`、`leases/`与专用CLI在当时已有后续未提交切片、联合定向9 passed；其余主体路径及完整默认/样例/doctor/selftest/CI映射仍缺。request/offer/proposal/工作checkpoint/watch等完整原语未完成，普通操作回执和同步checkpoint不能代替它们。手写message内容不等于系统已实现通用秘密识别。
- **ReadQuery@2**：children/replies的一层集合展开，各集合独立pageInfo/endCursor/next；根页与子页分别续读并重查主体/父级/子项当前权限。HTTP query-string、短路径与QueryRef已有同契约测试；旧@1不接受新增expand字段。展开时根页最多10、nested_first为1–10，成本公式受限；这不是任意递归查询。完整GraphQL/CLI/MCP等价、全部深度/节点/字节/时间预算、投影/缓存/错误矩阵及客户端分页仍待验收。
- **logo**：README与网站入口使用极简标识，源码包含明暗SVG与favicon；本地测试覆盖HTML/Markdown协商、无脚本CSP、favicon只读/HEAD及root托管样例。它是展示更新，不代表完整Web/TUI或生产可见性；本批构建通过，但未据此声明生产可见。

最新云文档还有货币/BankRole/账本/Entitlement及相应并发、隐私、备份验收要求；当时已有货币基础/Listing/寄售包未提交代码，尚未完整验证，不以“免费服务”或早期不做钱包的记录排除这一需求。默认MSG等字段以最新权威正文为准，不能把logo/协作切片写成补齐货币功能。

## 影响公开发布的关键阻塞

1. **完整性与可恢复性**：托管历史密文仍依赖旧vault，映射/签名ACK不等于可安全销毁旧钥；维持finalize拒绝。v4仅接v4，恢复写暂停与worker/daemon禁外发marker须人工核验提升，根秘密另备。
2. **存量授权治理**：旧签名CA不自动包含sharing.basic或webhook.domain等新增权限。必须在隔离环境明确重签/轮换、旧链失效和客户端迁移，不自动改生产。
3. **外部环境证据**：Webhook公网、SMTP、SSH、工具隔离、生产备份并发尚未完整实测。本机HTTP/浏览器探针只证明对应场景；禁JS页面未发请求与opaque探针实际GET被403/CORS拒绝是两种证据。
4. **协议/功能覆盖**：复杂文件编辑、多来源分享、完整读取/TUI与协作原语尚缺。若不改变权威需求，就只能按局部功能交付，不能称全量项目完成。

## 历史计划：下一批可独立验收出口

- 本批423 core/8 conformance/build已本地通过，先明确提交/CI归属；补handoff/lease主体只读路径与CLI，以无副作用/当前授权/分页/状态冲突为出口，不扩成工作流。再把ReadQuery@2推广到全部公共入口并验证相同预算、错误与续页语义。

- 当前batch/分享已通过414项本地验证并合并main，main CI36302710481成功；继续保留失败整批不发布新引用、转授来源失效、DM/system旁路负例。Git不可达孤儿按引用保留/回收规则处理，不声称已自动清零。后续补历史generation映射及超出read/空constraints的分享能力时需新契约验收。
- 对Manifest逐feature补默认/样例/doctor/selftest与CI映射，明确disabled/partial含义，不用API存在代替完成。
- 独立做生产同版本的隔离backup/restore、禁外发、CA快照差异演练；不以演练授权生产切流。
- 再补完整Markdown/结构化patch、跨表示rebase和完整读取/客户端，避免同时扩大身份恢复或开放托管JS。

需求修改建议另见 [DESIGN_CHANGE_REQUESTS](DESIGN_CHANGE_REQUESTS.md)。未获用户决定前，本报告仍按原设计记录差距。

历史合并记录：[PR #63](https://github.com/TokenNotIncluded/msg.lmm.best/pull/63)、[main CI 36302710481](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36302710481)。本轮最新代码的CI以 [36318026014](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36318026014) 为准；生产仍未部署。
