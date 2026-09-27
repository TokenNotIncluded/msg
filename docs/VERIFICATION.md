# 本地与 CI 验收记录

## 当前工作树进度与验收边界

本次实时读取[ChatGPT权威设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)修订 `ANLCKQmHD9c-ju8…`，191个非空段；[已完成部分](https://docs.google.com/document/d/1FtTdF5uhBPAsi-so-jOfpsiVI19RWKgx6bzIEFvpR2E/edit)当前修订`ANLCKQmjW4TG7Ecy…`、71段，已含A19/A20等后续归档。此前189/71及更早196/19、首次5段+4句仅为历史迁移记录，不能将全部71段归为那次迁移；后续用户归档不取消回归要求。

`fb9bb65` 的[CI36310779270](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36310779270)完成502 core/8 conformance/build；`3bc06ad` 的[CI36311726265](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36311726265)完成503/8/build。最新 `c919216` 的[CI36313186799](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36313186799)已completed/success：**505 core / 8 conformance / build通过**，只归该提交。`identity.temporary@3`要求客户端创建前持有独立Ed25519/age钥，服务端验持有证明并同时登记公钥，升级复用双钥；旧@1/@2拒绝新建。历史无钥临时主体仍须双钥升级，临时签名凭据当前登记但业务继续用受限token；本批未生产部署，不能以CI代替存量迁移与完整恢复验收。fb9的手动市场链仍不等于官方market_e2e。

归档A18的首次DM介绍已由3bc06ad成功CI验证并移回已完成部分。A01仍限定正式注册/托管创建：临时双钥@3已通过c919成功CI，仍待存量迁移及临时签名凭据能力验收；A16复杂GET短码和完整模板/示例仍在待办。

| 本批切片 | 当前实现事实 | 明确未完成项 |
| --- | --- | --- |
| 临时身份双钥 | @3客户端预存独立签名/加密钥，服务端PoP后同时登记；旧@1/@2新建拒绝；升级复用 | c919 CI已通过；旧无钥临时主体升级、临时签名凭据的业务能力及全部恢复/迁移矩阵仍缺 |
| 本机money与配置 | mint/burn、BankRole授撤、root transfer、offer set/disable，确认/根审计；MoneyConfig默认值和doctor检查 | 无purchasable类型/兑现器，offer set仍fail-closed；bank fund便利入口、真实物理控制台和生产恢复验收未据此完成 |
| ServerOffer | 安全空目录；无有效类型/兑现器则redeem拒绝且不扣款 | 真实报价启用/ResourceEntitlement/异步settle-refund未完成；报价管理代码不等于已有可售资源 |
| 凭据恢复 | 默认15m、1–60m可配；恢复后代继承原deadline、不扩大期限；备份恢复保留配置 | 旧@1重复秘密、全部适配器秘密URL拒绝、恢复响应再次丢失完整矩阵、历史钥在线/备份退役仍缺 |
| Bounty | 预算预托管、当前IdentityKey PoP/nonce/TTL、原子Claim与奖励、top_up、close剩余退款 | 完整账户分型/全部公开投影/CLI/default/doctor/selftest；不宣称全部并发/撤权/恢复矩阵齐备 |
| Sale订单与退款 | 签名买家入系统escrow、funded状态；funded买家取消原子退款 | 完整客观故障退款、争议/仲裁/申诉及所有状态组合未实现 |
| 手动站内交付 | managed_instant且合计≤1MiB，买家显式签名prepare、只读get、签名accept校验digest后结算给seller | 非自动交付；大型Transfer、sealed_manual/secret加密交付、Email/DeliveryTarget四方校验、全部商品类型/状态生命周期未完成 |
| HTTP读取 | money/order/delivery只读路径已接已有授权契约 | 完整纯路径/所有表示/缓存/错误/字段裁剪与分页矩阵仍待验收；GET不能prepare、claim或settle |

### 安全与语义边界

资金按整数minor_units入不可变PG账本，当前写事务串行化保护余额/总量/并发，签名收据有policy_version/digest/sequence。不能以此宣称完整ClearingPolicy/恢复矩阵完成。本机重复手动执行使用新的request_id，不能套用同ID网络重试的防重复承诺。

Order/Bounty escrow仍以无Resource/key/credential的local_only system Subject承载，普通money.transfer拒绝system账户；设计要求系统LedgerAccount、非用户。专用账户分型及注册/恢复/委托/银行管理不能激活托管身份的完整负例尚缺，不能只凭没有钥认定全部隔离完成。

Bounty单项store.listing_get的当前state/pause_reason/budget已与bounty.get一致；历史Revision保留原值并附current_*。搜索与其他投影仍待全面验证。奖励支付复核当前bounty事实，未来缩小写锁需重测最后一份预算和主体限额。

资金现在不再只有funded入口：funded可取消退款，支持范围内的买家显式prepare/get/accept可结算。但这些不是自动Delivery；无Email endpoint绑定、四方owner/撤销复核、邮箱pending验证/最小邮件/secret禁明文SMTP完整流程。只读交付不自动claimed，accept由买家明确签名，不能把HTTP成功或SMTP接受等同收货。

`test_market_manual_flow.py` 已串起Test Root内部mint/银行登记/注资→10MSG预托管PoP奖励→5MSG固定bundle购买→买家显式prepare/get/accept→bank15/buyer5、total_supply20。它使用内部本机用例和手动交付，**不证明官方market_e2e要求的bank fund命令、自动Delivery、SMTP sink/未验证邮箱边界及全部失败矩阵**。DisputeResolver/仲裁和大型Transfer也仍缺。

下一出口：3bc的DM@2已通过CI；临时双钥@3已通过CI，继续存量迁移验证；再补系统LedgerAccount分型及跨模块备份/恢复、自动交付与状态故障恢复、大型Transfer；随后按买家身份绑定接Email并以本地sink验秘密/字段裁剪，最后跑官方market_e2e和仲裁独立矩阵。任何中间成功不自动授权生产交易或解除未兑现报价的拒绝。

## 历史：文档迁移与此前验证归属

已实时读取[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)迁移后修订 `ANLCKQmFOHtA75…`，196个非空段；以本节和当前正文为准，以下08:51等记录属于修订沿革，不再是最新基线。

用户要求的瘦身已由主执行者完成：迁移前201非空段、迁移后196；[已完成部分](https://docs.google.com/document/d/1FtTdF5uhBPAsi-so-jOfpsiVI19RWKgx6bzIEFvpR2E/edit)保存Todo、handoff、lease、presence、claim五个整段，及LinkSet入口、GraphQL读写分离、短码不复用、atomic batch四句。已读回归档内容；主执行者报告逐段期望变换完全匹配。归档依据e126539与[成功CI36306888836](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36306888836)，仅表示这些条款完成，未迁出的条件和所有回归要求仍有效。

当前未提交下一批（协作路径/CLI、货币基础、sale Listing/固定文件寄售包）此前完整本地 **432 core passed、8 conformance passed、uv build成功**；这发生在Listing mode小修之前。其后显式mode=sale、旧记录按sale兼容、bounty返回listing_mode_unsupported，store定向5 passed、综合定向40 passed；未声称修后全套再次通过。9/38/16定向组已包含在全套，不累加。e126539的423/8/build及成功CI归旧提交，不能当作432项工作树CI；没有本批生产部署或market_e2e完整闭环证据。

### 新规范已生效、实现仍需补齐

DCR-01/02/03主要建议已写入权威正文并进一步扩充，详见[变更记录](DESIGN_CHANGE_REQUESTS.md)，不再统称待批准。

1. 所有等价可重放秘密禁止URL；私有纯路径proof需绑定查询/主体/ID/expiry且重验授权。机密发行无安全通道返回secure_channel_required；接收者绑定密文、敏感查询保密、遗留服务端token URL分支与全入口/日志拒绝矩阵尚需验收。
2. 一次释放先原子消费资格、再发送响应；默认15m从业务提交起算，配置有界且重试不续期。@2/journal已有切片，旧@1重复秘密仍是实现缺口；可配置窗口、恢复再次丢失、原谱系撤销/不扩权全矩阵不能仅凭现有测试称完成。
3. 历史A类rewrap副本与B类仅退役EncryptionSubkey的RecoveryEnvelope均须明确边界；冻结全保留历史、并发增量、新写入新钥、实际逐项解密ACK、identity_switched/history_recoverable/server_key_retired及online_retired/backup_retired、旧备份防复活仍未完整实现。旧vault仍可解密时不能宣称完整退役。

bounty、完整清算/托管、Order/Delivery/仲裁和官方market_e2e仍缺；本地432项覆盖既有切片，不把sale基础或归档条款扩写为整章完成。

## 08:51 需求新增记录（历史；当前进度见文首）

权威基线更新为 `2026-09-27T08:51:45.979Z`（198段，01–15章）。当前未提交Listing/寄售包只对应sale基础，不能视为Listing mode=sale|bounty完整支持。货币相关38、store相关16、协作9项定向是局部证据，core全套已432 passed、conformance8 passed、build通过；e126539的CI36306888836已success（仅覆盖旧提交），不冒称本批完整通过。

- **Bounty**：publisher在激活前将budget原子转入BountyEscrow；它是无私钥系统LedgerAccount。固定reward/claim_limit（默认1）/max_claims/eligibility/verifier版本，预算不足自动paused/out_of_budget，可显式top-up；publisher离线仍发奖，不依赖事后手动付款。
- **signature_pop_v1**：服务端随机challenge绑定listing、claimant、nonce、issued/expiry与verifier版本，当前IdentityKey签名；校验签名/TTL/nonce单次消费/状态/eligibility/次数。只证明控制该主体当前签名钥，不证明真人/唯一自然人或反女巫身份。BountyClaim成功与奖励转账、receipt/Event/通知同事务，最后一份预算并发只成功一次，不得先记paid再转账。
- **checkout邮箱**：新未验证地址最多收到不含订单号、用户名、商品或提货能力的验证消息；站内交付照常完成，Email pending，验证后才能绑定。已验证且属于buyer的地址方可直接作为目标；worker继续复核四方主体/撤销/订单状态。邮件关闭或失败不阻止订单；不得把现有email验证功能当作该订单绑定流程已经完成。
- **market_e2e固定出口**：隔离Test Root mint 20 MSG并bank fund 20；bank预托管10奖励，buyer完成PoP得10，余额bank=10/buyer=10/bounty escrow=0；再买5 MSG固定bundle（指定文本与hello.txt内容），完成buyer→OrderEscrow→bank及自动Delivery、settled、摘要核对，最终buyer=5/bank=15/全部escrow=0/总量20。重复、过期、错主体/错key、重放、最后一份预算竞争均须拒绝且不重复扣款。SMTP sink/FakeNotificationSender验证最小邮件，默认不发真实外部地址；真实SMTP另验。

上述预算账户、挑战状态、Claim与订单/交付都必须作为PG权威事实一致备份恢复。当前没有该官方fixture完整闭环通过证据；普通签名transfer、sale寄售包或旧成就挑战均不能替代它。

**当前状态：`e126539981508cf25cda6ab9bbc9b3531449d203` 已提交推送，包含此前423 core / 8 conformance / build通过的协作底座、ReadQuery@2与logo；[CI 36306888836](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36306888836)已completed/success（仅覆盖e126539）。当前main工作树另有未提交增量：协作主体路径/CLI联合定向9 passed；货币基础相关定向38 passed、/store Listing/寄售包相关定向16 passed；当前core432、conformance8、build通过；尚无432项工作树CI或部署证据。**

## 最新修订与下一批进度

已实时重读权威设计 `2026-09-27T08:51:45.979Z`（198段，01–15章）。本轮进一步明确ClearingEngine为msgd内置确定性程序：不是subject，无账户/余额/私钥/主动交易或自由裁量；同输入、账本状态、ClearingPolicy版本必须同结果。货币receipt含policy_version/digest与ledger_sequence，纠错只能追加refund/reversal。EscrowAccount是系统LedgerAccount而非用户；EscrowEngine只按订单状态和有效决议执行，仲裁员不能写Ledger。

- **已推送基线**：e126539的423 core、8 conformance、build为此前本地结果；CI 36306888836已success，不借旧main CI证明本提交成功，也不覆盖当前增量。
- **协作入口增量**：`/@user/handoffs/`、`leases/`及单项只读视图、专用CLI已有工作树实现，联合定向9 passed。读路径复用已有Operation/授权，不能以该切片宣称全部协作原语、全部客户端/路径、feature默认/doctor/selftest或整章已完成；尚未提交，已纳入432项本地全套。
- **货币基础增量**：money.state/banks/balance/ledger/transfer与PG账本字段已有源码；可见policy_version/digest/ledger_sequence等初步记录。相关定向38 passed，覆盖普通签名转账、不可变PG账本、零发行/余额/总量、并发双花/幂等、签名收据和root网络拒绝；不等于完整ClearingEngine或全部货币验收。本机msgd money发行/销毁/BankRole/报价、redeem/Entitlement与完整恢复矩阵仍缺。
- **/store sale基础增量**：sale Listing create/update/get与package_deposit/get已有工作树代码，相关定向16 passed，覆盖空/store、Listing修订/价格/条款、seller-only固定文件ConsignmentPackage与通用操作绕过拒绝。它们不是完整购买/结算/交付：Order、PaymentIntent/ClearingEngine完整契约、EscrowAccount/EscrowEngine、DeliveryTarget/Delivery、纠纷/仲裁及相关CLI/只读路径仍缺。

下一步本批432/8/build已本地通过，下一步明确提交/CI归属；9/38/16定向组可能重叠，不累加为独立总数；货币按精确账本和确定性ClearingPolicy→本机中央银行/BankRole→报价兑换→Listing/不可变寄售包→Order/PaymentIntent/Escrow→买家交付/权限视图→客观退款和确定性仲裁推进。每步分别验证并发幂等、权限/失效、默认/doctor/selftest与一致备份，不把进行中的代码计作完成，也不自动启用生产交易。

### 08:51 权威需求与仍缺范围

本次按 Drive 返回的修改时间 `2026-09-27T08:51:45.979Z` 重新读取同一权威文档（198段，01–15章）。相对旧报告，货币与寄售市场是实质新增范围，不能沿用“无钱包/Store待定”来排除：基础身份/公开读/普通通信免费，货币不购买认证、CA、系统权限或优先级，禁止法币充值提现与收益承诺。

- **货币**：精确minor_units/scale=6、primary稳定ID、余额/双边账本/总量守恒；@root仅本机mint/burn/BankRole/转账/报价，银行不增发不透支；transfer/redeem、价格快照、Entitlement与pending/settle/refund。当前货币基础已有未提交38项相关定向通过；全套432 core/8 conformance/build已本地通过，其余契约仍缺。
- **寄售与订单**：/store Listing与不可变ConsignmentPackage；订单固定listing/package/价格/条款版本，随机不可枚举order_id且无权与不存在等价。managed_instant、sealed_manual与service交付不同；不是已有帖子/Transfer换个名字即可满足。
- **资金托管与仲裁**：buyer→Escrow→seller/refund/split原子记账；版本化客观故障处理，Arbitrator只签Decision不能改Ledger，确定性panel/quorum/利益冲突排除和限定appeal。不能借管理员或AI自由裁量补空白。
- **发货与隐私**：权威交付在买家订单/_delivery，Inbox只给最小引用，Email仅可选通道；下单锁定买家已验证endpoint，发货重新核对buyer/DeliveryTarget/邮箱owner/加密钥owner；seller不获真实邮箱，secret不明文SMTP，claimed不等于SMTP送达。
- **验收与持久化**：PG保存权威货币/订单/Escrow/交付/仲裁事实并一致备份；默认零发行量/余额、无银行/报价、空store与订单集合。双花/幂等/价格修订/授权裁剪/退款/交付错配/仲裁边界/恢复均须独立测试、doctor/selftest与CI。此前e126539的423 core、8 conformance与build只覆盖该提交已有功能，不覆盖当前未提交货币/市场契约，也不证明整章完成或生产部署。

## 2026-09-27 handoff/lease、ReadQuery@2 与极简 logo（e126539已推送，CI进行中）

已重读权威需求最新修订 `2026-09-27T08:51:45.979Z`。代码已提交推送为 `e126539981508cf25cda6ab9bbc9b3531449d203`，CI36306888836已success；执行者报告完整本地 core **423 passed**。该批准确耗时/完整命令未在此重复补造；**conformance 8 passed，uv build 与 git diff --check 均通过**，此前414/8/build及其main CI不能充当本批证据。

本批core包含 handoff权限过滤/状态决策并发/最小通知、lease TTL/generation/非排他/只读到期、ReadQuery@2独立子集合分页/当前授权/旧版本拒绝/成本边界，以及logo内容协商/静态只读/CSP检查。定向不重复累加为新的总数。源码可见不等于已发布包或生产部署；协作主体路径和CLI已另有未提交9项定向切片；完整feature默认/doctor/selftest、全协议嵌套读取及真实浏览器/宿主矩阵仍缺。

## 2026-09-27 text_patch@2/rebase/batch与ShareGrant@2（PR #63已合并main，CI成功）

GIT_CONFIG_GLOBAL=/dev/null下全套 **414 passed**、conformance **8 passed**，`uv build -q`成功；PR #63已合并main，merge commit `2b4d483d58dfe4eb2d81565377238dbb5e17a6b5`；[main CI36302710481](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36302710481)completed/success（414 core、8 conformance、build），未部署。content.text_patch@2显式rebase及text_patch_batch、ShareGrant@2 group/read-only空constraints/受限reshare已纳入；旧@1不变。批量保证SQL引用原子发布，不保证Git/文件系统回滚，可留Git不可达孤儿；历史generation映射仍有缺口，分享@2不支持任意操作或非空constraints。

基础提交52dd4b4已推送，[CI 36301644644](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36301644644)completed/success（408 core、8 conformance、build），只归该旧提交。该旧CI不替代当前main CI。PR已合并，本地已切main并git pull --ff-only同步；部署仍未完成。


## 2026-09-27 BootstrapManifest v5、组织治理与post操作（未提交，本地全套通过）

GIT_CONFIG_GLOBAL=/dev/null下全套 **408 passed**、conformance **8 passed**，`UV_CACHE_DIR=/tmp/msg-uv-cache uv build -q`成功；短码200→215旧义保留。未提交、无本批CI。Manifest v5的12项feature及doctor/selftest真实映射、组织四策略/三角色/旧组织兼容/虚拟public、post metadata/rollback与write/patch别名已纳入。disabled/partial只标完成度，不关现有API。

完整TDD feature矩阵、rebase、atomic batch等未完成。前批2a80a95的CI36300451977已completed/success（392/8/build），不覆盖本批。


## 2026-09-27 backup v4、text_patch与Domain Event Webhook（2a80a95已推送、CI成功）

GIT_CONFIG_GLOBAL=/dev/null下全套 **392 passed**，conformance **8 passed**，`UV_CACHE_DIR=/tmp/msg-uv-cache uv build -q`成功；短码196→200旧义保留。已提交2a80a95，CI36300451977 completed/success（392/8/build）。backup v4验证PG/Git/CAS/LFS引用，隔离restore写暂停及worker/daemon禁外发marker，需显式人工提升；只接v4，root秘密另备。生产在线备份仍未演练，外部Git写可能导致fail-closed。

content.text_patch为exact/context唯一匹配、1MiB上限，无rebase/batch。Domain Event Webhook只允许post_create/reply/post_edit的owner显式订阅，公网实测未做。旧e89b972的379/8/成功CI不覆盖本批。


本批重新完整读取权威文档2026-09-27T05:54:08.096Z（185段01–15章），主要为压缩去重，不减少逐字段/状态/路径等验收要求。v4归档版本和恢复drill硬闸为本地实现，非权威指定版本。独立webhook.domain capability已补：owner/ACL与证书每次投递复核，无cap拒绝、撤销停投；公网矩阵仍未验收。

## 2026-09-27 Inbox Webhook、只读TUI与Search suggest @4（e89b972已推送、CI成功）

GIT_CONFIG_GLOBAL=/dev/null下全套 **379 passed**、conformance **8 passed**，`UV_CACHE_DIR=/tmp/msg-uv-cache uv build -q`成功；短码192→196旧义保留。已提交e89b972，CI36298075880 completed/success（379/8/build），不借用e18d0b6的363/8/build CI。

Webhook为Inbox opt-in、vault封存secret，HMAC/去重/SSRF/uncertain定向已纳入全套；真实公网/Domain Events未验。TUI第一片Home/Inbox/Search/Thread只读、零自动ACK。Search@4显式suggest，spell未做。以上不等于完整Webhook/TUI/Search feature或生产部署。


## 2026-09-27 SearchQuery@3、共享LFS CAS与hosting CLI（e18d0b6已推送、CI成功）

权威修订实时核实仍00:35:34.164Z。GIT_CONFIG_GLOBAL=/dev/null下全套 **363 passed**，conformance **8 passed**，`UV_CACHE_DIR=/tmp/msg-uv-cache uv build -q`成功；已提交推送e18d0b6，[CI 36297201348](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36297201348)completed/success（363/8/build）。联合定向28包含在全套内，不累加；短码191→192保旧义。SearchQuery@3新增source_kind/relation_type，q/3/query-string/QueryRef同版本。LFS新对象共用blob_dir CAS、repo hardlink GC根、显式pin保留修复；PG串行默认4GiB准入与共享卷sentinel仅限LFS新对象，旧repo迁移及全worker staging预算未完。hosting CLI preview/deploy/activate/history使用签名header及惰性输出文件限制。

前批81ec32a已提交，本地355/8/build，CI36296327583已completed/success（355/8/build），不能归作本批通过证据。


新LFS对象共享Files/Transfer BlobStore已有；旧对象、全部署staging和其它CAS写入预算未完成。hosting preview CLI禁裸URL、使用签名header且惰性写输出；Search@3来源/关系过滤不包含suggest/spell。

## 2026-09-27 Search facets @2、路由effect与权限快照（81ec32a已提交，CI通过）

GIT_CONFIG_GLOBAL=/dev/null下全套 **355 passed**，conformance **8 passed**，`UV_CACHE_DIR=/tmp/msg-uv-cache uv build -q`成功；已提交81ec32a，CI36296327583 completed/success（355/8/build）。lexical_search@1旧schema保持，@2新增facets，QueryRef/续页/HTTP query/q2共用@2；短码190→191旧义保留。路由effect矩阵及只读doctor.authority_snapshot已纳入。

诊断仅揭示旧Root/Online CA冻结grants差异；不原位重写签名、不自动扩权或改生产。显式Root轮换会使旧链失效，需先评估重签/迁移。前批78dbd80的CI36295518895已completed/success（351/8/build），不是本批证据。


## 2026-09-27 真实LFS、ShareLink与托管历史副本（78dbd80已推送、CI通过）

GIT_CONFIG_GLOBAL=/dev/null环境全套 **351 passed**，conformance **8 passed**，`UV_CACHE_DIR=/tmp/msg-uv-cache uv build -q`成功；已提交推送78dbd80，[CI 36295518895](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36295518895)completed/success（351/8/build）。联合定向24包含在全套内，不累加；短码183→190旧义保留。真实git-lfs **3.8.0**以 **1.3MB** 对象完成push/clone/pull，Range与/-/.git兼容已有。未完成跨worker配额、共用BlobStore/GC生命周期。

ShareLink默认off，受控system.share_links_set开关；token仅POST body，GET长短路径均拒绝，不是裸URL分享。托管历史age Revision仅创建显式私有新钥副本+mapping，原历史不变、finalize继续fail-closed；外部密文不可证明。以上不借用8480589的CI。


ShareLink长短GET token URL封禁与system.share_links_set签名开关已有回归。历史密文只形成显式私有副本与mapping，finalize仍关闭，不声称全账号迁移或服务器能证明外部密文可恢复。8480589的CI36294545663已success，属于前批345/8/build。

## 2026-09-27 持久Sync checkpoint与最小LFS（8480589已推送、CI成功）

权威设计修订仍为00:35:34.164Z。当前Sync定向 **10 passed**、LFS定向 **8 passed**，随后GIT_CONFIG_GLOBAL=/dev/null环境全套 **345 passed**、conformance **8 passed**，`UV_CACHE_DIR=/tmp/msg-uv-cache uv build -q`成功；此前定向不额外累加，仍未提交/无本批CI。签名checkpoint open/ack经/-/、GET pending只读、CAS与精确seen最多10000；LFS普通repo仅download，上传/-/校验SHA256/size后原子发布。真实git-lfs、Range、跨worker配额、与Files/Transfer共用BlobStore均未完成验收。

托管升级冻结清单/映射/新钥签名ACK已有，但历史Revision仍依赖旧vault，finalize_ready=false，不记完整升级。前批c965385已推送，本地336/8/build，CI36293461291失败；旧结果不覆盖当前切片。



失败根因：默认Git 1MiB postBuffer先发送b'0000'探测，再同request_id发送真实包，原处理造成409。本批probe改为只读鉴权、不建job或业务幂等结果；GIT_CONFIG_GLOBAL=/dev/null联合定向 **27 passed**。短码175→183、旧义保留。本批最终GIT_CONFIG_GLOBAL=/dev/null全套345/8/build已通过，真实默认buffer两POST回归通过；仍未提交/无本批CI，不能把c965385失败CI改写为成功。LFS签名PUT与托管冻结清单ACK同属本批未提交切片。


提交8480589已推送，[CI 36294545663](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36294545663)completed/success（345 core、8 conformance、build）。c965385的失败仍是历史事实，默认Git探测修复在8480589获得远端验证；未部署。

## 2026-09-27 Todo due、custodial rewrap与Git流式限制（c965385已推送，CI失败）

第五批`.venv/bin/python -m pytest -q`为 **336 passed**，conformance **8 passed**，`uv build -q`成功；已提交推送c965385，CI36293461291失败。Todo到期维护任务定向 **8 passed**，仅本人Inbox且去重；custodial单条age rewrap定向 **11 passed**，保留旧vault/token、client_decryption_verified=false，不自动收尾；Git定向 **10 passed**，/-/receive-pack独立32MiB、每worker2并发、staging流式SHA256及64KiB feed。上述定向已包含在336项全套，不额外累加；短码173→175，旧义保留。

未实现/未证明：LFS、多ref原子性、跨worker磁盘配额；rewrap不等于客户端已解密验证或完成托管升级。旧d4affbb本地332/8结果不覆盖本批。


## 2026-09-27 hosting preview、Notes/Todo、ShareGrant与Sync边界（d4affbb已推送，CI已通过）

当前 `.venv/bin/python -m pytest -q` 为 **332 passed**，conformance **8 passed**，`uv build -q` 成功；已提交推送d4affbb，[CI 36292686957](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36292686957)已completed/success（332/8/build）。已报定向hosting **21 passed**、notes **4 passed**、sync **1 passed**；ShareGrant直接叶资源限时read/revoke/list已实现并通过定向，以上不相加成全套。真实root web Resource与private preview、Notes archive/restore/私有Todo、ShareGrant直接叶资源和Sync失败边界各有代码切片，但完整feature仍未完成。

preview需签名header，不是无凭据浏览器链接；Todo尚无到期本人Inbox，Sync长期checkpoint/ack未实现。前一ae3aac2的326/8/build/CI成功不能作为本工作树证据。



最终合并定向28项已包含在332项全套内，不重复累加；短码162→173且旧码意义不变。真实DNS light.local解析10.174.197.165，隔离服务18147上/@root/web/index.html的GET/HEAD均200、含CSP sandbox；普通POST到web/main均405。临时数据库、服务、文件已清理，端口无监听。此为本地HTTP证据，不是生产部署或完整浏览器preview验证。

ShareGrant直接叶资源限时read/revoke/list、私有Note单项读取不授父目录列举；排除SOUL/Todo/DM/system/preview。旧安装Online CA冻结grants不会自动取得sharing.basic，启用前必须受控重签并验证权限范围，不能直接沿用新安装证据。旧ae3aac2的CI成功不覆盖本批。

## 2026-09-27 规则源迁移与标准客户端token @2（ae3aac2已推送、CI通过）

提交ae3aac2前 `.venv/bin/python -m pytest -q` 为 **326 passed**，conformance **8 passed**，`uv build -q` 成功；提交ae3aac2已推送，[CI 36291771557](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36291771557)已success，未部署。规则源显式迁移保留rule_id/Resource/历史，完整清单/依赖先校验，缺失/未知/重复/悬空声明拒绝。标准客户端默认token @2，0600 journal与显式`msg identity recover-token`，不降级@1。

恢复秘密只用HTTP/GraphQL/MCP HTTP body；PathGET拒绝。真实域要求HTTPS，测试/loopback例外仅testserver、localhost、127.0.0.1、::1；light.local历史HTTP探针是非秘密读取测试，不证明token安全传输。当前没有生产部署证明。


## 2026-09-27 CLI Search/Grep、Sync resync 与 token @2（097b252已推送、CI通过）

提交097b252已推送，[CI 36291133946](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36291133946)已completed/success；以下为该提交本地证据。`.venv/bin/python -m pytest -q` 为 **319 passed**，conformance **8 passed**，`UV_CACHE_DIR=/tmp/msg-uv-cache uv build -q` 成功。短码snapshot由157增至162项且旧码意义不变。此前CLI/Sync定向11、token相关20已包含在全套内，不额外累加；不借用c62e516已通过的309/8/build CI。

CLI search/grep采用单页与显式cursor；Sync授权epoch/Topic成员摘要变化要求resync，只披露已知撤权最小ID且不给续cursor；>64引用明确失败。50个seen叠加完整路径proof可能超过URL限制返回413，已有header proof可用，但纯路径大窗口仍有限。

四种token发行操作新增@2严格原子claim，独立至少32B恢复材料窗口最多15分钟；identity.token_recover消费旧恢复材料、撤销旧token并保持原scope/期限换发。定向覆盖一次交付、并发、丢响应恢复、重启和过期。该提交时旧@1及默认客户端仍未切换；后续326项工作树才增加客户端默认@2，不能声明全平台一次展示完成。


## 2026-09-27 SearchQuery、Grep、Legacy 登记与 CLI 批次

权威需求仍为 Google Drive `ChatGPT` 文件夹《msg.lmm.best｜项目设计》，修订时间 `2026-09-27T00:35:34.164Z`。提交 `c62e516` 前本地 Python 3.15/PostgreSQL 下 `UV_CACHE_DIR=/tmp/msg-uv-cache uv run python -m pytest -q` 为 **309 passed**，conformance 为 **8 passed**，`uv build` 与 `git diff --check` 通过。新增受限词法 SearchQuery、限定已知范围的 Grep、搜索 QueryRef 分页、纯路径读取等价；旧 `discovery.search@1` 契约及已发布短码保持不变。针对 2001 条范围外资源，新增先红后绿回归：scope 在 SQL 候选阶段限定，预算只计当前可见且通过基础筛选的结果。Grep 仍是受限正则、无分页；facets、suggest、语义搜索、关系/来源筛选等未完成。LegacyDirective 只登记本人签名声明，不执行遗言或授权；私有历史不能变为公开，选定历史版本也检查其当时的可见性。CLI 增加 DM、Recovery、Legacy 入口。

隔离测试实例以本机 DNS `light.local` 解析的 `10.174.197.165` 监听 `18146`，真实 HTTP 请求 `/`、旧搜索、`/_s/q/2/...` 新词法搜索和 `/_search/grep` 均为 200；普通 `/main` POST 为 405。首次探针把旧搜索参数写成 `q`，收到预期的 `unknown_query_parameter`；改为契约参数 `query` 后，又发现测试实例原先的认证目标仍为 `testserver`，调整探针实例目标地址后最终通过。临时探针文件、数据库和服务已清理，端口无监听。此结果证明本地路由与 Host 端到端可用，不代表生产部署或浏览器中更广泛的安全矩阵。提交 `c62e516` 已推送；[远端 CI 36290261781](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36290261781) 为 completed/success，309 core、8 conformance、构建通过。尚未部署。当前后续 CLI Search/Grep 与 SyncCursor 改动尚未提交，不能借用该 CI。

## 2026-09-27 托管升级与同域托管批次

权威 Google Drive `ChatGPT` 文件夹设计仍为 `2026-09-27T00:35:34.164Z` 修订。提交 `d365858` 前本地 Python 3.15/PostgreSQL 下 `uv run python -m pytest -q` 为 **295 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 与 `git diff --check` 通过。托管升级已测试新 Ed25519/age-X25519 双钥持有证明、本地待提交 journal、已知 age 密文存在时的 `pending_rewrap`、空已知库存时同事务新钥绑定/旧 token 撤销/vault 销毁/审计，以及最终响应丢失后用新签名钥查询结果；外部密文迁移仅记录本人声明，逐对象 rewrap 尚无通用流程。托管静态文件改由主域匿名只读服务，HTML/HEAD/304/206/错误响应强制 CSP sandbox，本批完全禁用脚本；危险格式强制下载。

真实浏览器使用本机 DNS 直接访问 `http://light.local:18144/@browser-probe/web/index.html`，不是 `--resolve`：页面快照仍为 “script not run”，浏览器控制台明确提示 sandbox 未允许脚本，网络记录仅 HTML GET，产品托管页**没有发出**私有 API 请求。另起受控探针页 `http://light.local:18145/` 并设 `sandbox allow-scripts`，其不属于产品托管响应；它从 opaque `Origin:null` 对 `http://light.local:18144/_read/t_private/json` **实际发出** GET，服务端访问日志为 403，浏览器报 CORS 无允许来源，脚本不能读取响应。此前 data: 源探针因浏览器 Private Network Access 在发出前拦截，不用作服务端拒绝证据。测试服务按预定时间退出，浏览器会话、临时数据库/目录和端口已清理。当前结果不证明 JS 托管、候选部署 preview、所有重定向/Service Worker/凭据组合或完整同域安全隔离。提交 `d365858` 已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36288621652)已通过；尚无线上部署结果。

## 2026-09-27 托管身份、SyncCursor 与显式 rewrap 批次

权威 Google Drive `ChatGPT` 文件夹文档读取至 `2026-09-27T00:35:34.164Z`。提交 `fcf6ae9` 前，本地 Python 3.15/PostgreSQL 下 `uv run python -m pytest -q` 为 **284 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 与 `git diff --check` 通过。托管创建使用独立 Ed25519/age-X25519 私钥和域分离 AES-GCM vault；受控内容写入的 Revision 由 vault 签名并标明 custodial 来源，未接通代签的 token 写入明确拒绝。`/_read/s`/`/_r/s` 使用独立 SyncCursor，已见引用集合加密后由 MAC 保护，撤权只返回最小 ID，最多跟踪 64 个引用。QueryRef 的私有描述 File 由维护任务在有效期后按引用边界回收，GET 不清理。另有显式选定 age keystore 条目的旧钥→新 recipient rewrap，保留旧 Revision 和旧钥，stale base revision 拒绝。完整托管升级/网络代解密、严格 token 一次展示及丢响应恢复、超过 64 引用的长期同步和权限新增后的旧事件回补尚缺；提交已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36287082964)已通过，尚无线上验证。

## 2026-09-27 QueryRef、发行来源与自托管恢复备份批次

权威 Google Drive `ChatGPT` 文件夹需求读取至 `2026-09-27T00:35:34.164Z`。提交 `65acff3` 前，本地 Python 3.15/PostgreSQL 环境下 `uv run python -m pytest -q` 为 **274 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 与 `git diff --check` 通过。新增复杂 ReadQuery 的 Transfer 分片封存、15 分钟 QueryRef 和短续页，读取逐次重验当前主体/源文件 ACL/摘要；QueryRef 的查询描述目前保留为私有 File，未自动清理。发行规则 Revision 的可选来源字段、history 游标与 `requires_rules[]` 已有回归。恢复切片由本人签名 opt-in Policy、绑定私有 age keystore 确切 Revision 的 Envelope 元数据及客户端标准 age 多 recipient 离线演练组成；两把指定恢复钥分别可解，无关钥不能解，解密不授账号权限。服务端将 recipient 集合标为 `owner_declared_unverified`，不据密文头声称已验证。账号恢复授权、旧密文 rewrap、完整 Custodial 双钥、QueryRef 描述自动回收等仍缺。提交已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36285859198)已通过：Ubuntu/PG16/Valkey，age 1.1.1 安装成功，恢复互操作测试实际执行，274 tests、8 conformance 和构建成功。尚无线上验证。

## 2026-09-27 源码规则、LinkSet 与个人文档批次

本批按 Google Drive `ChatGPT` 文件夹需求读取至 `2026-09-27T00:35:34.164Z`。提交 `8fdfb85` 前，本地 Python 3.15/PostgreSQL 下 `uv run python -m pytest -q` 为 **262 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 成功，`git diff --check` 通过。源码发行的短 `/AGENTS.md`、`/_rules` 索引和八个规则分片已打入 sdist/wheel，启动按文件 digest/version 幂等同步 Revision；`/wiki` 按普通签名资源治理。LinkSet、逐项授权的集合导航和真实 Revision diff，以及主动写入的私有 Notes/SOUL/主体 AGENTS 有定向回归。全套首轮曾因短 AGENTS 未列无凭据 `/-/p/<operation>` 模板失败；把源码规则版本升到 2 后，入口/规则定向与最终全套通过。发行来源字段的完整 Revision/history 投影、requires_rules、HTML/TUI 导航、个人文档独立 manifest 签名与完整秘密识别仍缺。提交已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36284478373)已通过；没有线上部署验证。

## 2026-09-27 Topic、被动 GET 与纯路径读取批次

权威 Google Drive `ChatGPT` 文件夹文档本批读取到 `2026-09-27T00:20:54.350Z`。提交 `befa5ee` 前的本地 Python 3.15/PostgreSQL 验证：`uv run python -m pytest -q` 为 **249 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 成功，`sh -n deploy/prepare-service.sh` 和 `git diff --check` 通过。本批含 TopicMembership/Ban 及真实治理 Event、HTTP 虚拟 `/_events.md`、`/-/g` 被动客户端防误触发、简单 ReadQuery/搜索的纯路径 q/1 与短码快照更新。隔离 selftest 曾因 Topic bootstrap 的测试子树建立顺序出现 `not_found`，调整种子顺序后定向与全套均通过。QueryRef、完整 RouteSpec、Topic 增量投影，以及最新新增的 `/_rules`、`/wiki`、SOUL/主体 AGENTS、LinkSet/SearchQuery 全范围仍缺。提交已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36282759370)已通过；未线上部署。

## 2026-09-27 双钥、主体别名与协议版本批次

权威 Google Drive `ChatGPT` 文件夹文档读取至 `2026-09-26T23:58:21.986Z`。本地 Python 3.15/PostgreSQL 环境下 `uv run python -m pytest -q` 为 **228 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 成功，`sh -n deploy/prepare-service.sh` 与 `git diff --check` 通过。新增 age/X25519 recipient 使用本机 `age-keygen -y` 与 `age` 加解密做互操作；注册/升级 v2 要求独立加密子钥，旧 v1 保留契约并明确拒绝缺钥注册；主体短长路径别名经真实钥、SSH、keystore、Inbox 数据验证。另含 DM、ReadCursor、presence/claim 与默认 1 MiB Git HTTP push 切片。提交 `e01dacc` 已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36281301900)已通过；custodial 双钥、恢复/遗言、Topic 治理、纯路径 QueryRef、新增 passive-client guard 等尚未纳入该提交，不能据此宣称完整需求或线上已更新。

`e01dacc` 另从临时独立 worktree、临时 PostgreSQL 和临时初始化服务，在本机 `10.174.197.165:18143` 监听，以真实 DNS 直接请求 `http://light.local:18143`（没有 `--resolve`）：`/`、`/AGENTS.md`、`/_r/query?root=/main&first=2`、`/_read/graphql` query 均返回 200，普通 `/main` POST 返回 405。服务按预设时间正常退出；worktree、临时数据库和目录已清理，端口无残留监听。这验证本地域名、Host 校验及本机 HTTP 路由，不代表生产部署或浏览器托管隔离。

## 2026-09-27 荣誉、读取游标与隔离 CA 自检批次

权威 Google Drive `ChatGPT` 文件夹需求本批读取到 `2026-09-26T22:52:52.714Z`。本地 Python 3.15/PostgreSQL 环境下 `uv run python -m pytest -q` 为 **206 passed**，`uv run python -m pytest -q conformance` 为 **8 passed**，`uv build` 成功；`sh -n deploy/prepare-service.sh`、`git diff --check` 通过。配置文件新安装改为 `msgd.toml`，旧 `server.toml` 单独存在时可读；隔离自检在 `/_test/<run_id>/` 验证 Test Root→L1→L2→L3→Leaf、越层/扩大部分授权和撤销。荣誉 R1–R5 目前仅 self-custody；ReadQuery 仅有 GET collection/PageCursor 切片。完整 CA 负例矩阵、托管代签、ReadCursor/SyncCursor、Profile 与成就索引仍缺。提交 `f085e7f` 已推送，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36278494686)已通过；未线上部署。

按用户提供的本地域名另做真实 HTTP smoke：在临时 PostgreSQL 与临时初始化服务上监听 `127.0.0.1:18142`，以 `curl --resolve light.local:18142:127.0.0.1 --noproxy '*'` 保持 Host 为 `light.local`。`/`、`/AGENTS.md`、`/_read/query?root=/main&first=2` 及等价 `/_r/query` 均为 200；普通 `/main` 的 POST 为 405，旧 `/!foo` 为 404；`/_read/graphql` query 为 200，`/-/graphql` 对 query 返回 `graphql_effect_mismatch`。测试服务已停止、临时库与目录已清理。这是本机隔离实例的 HTTP 验证，不是生产域名或同域托管浏览器隔离验收。

## 2026-09-27 ChatGPT 文档对齐批次

权威需求为 Google Drive `ChatGPT` 文件夹中的《msg.lmm.best｜项目设计》，本轮读取修订时间 `2026-09-26T22:19:27.354Z`，覆盖 01–15 章。当前工作树在 Python 3.15、PostgreSQL 测试环境下 `uv run python -m pytest -q` 为 **197 passed**；新增 CA、标签、只读入口等定向复跑 **29 passed**；修复只读 GraphQL 客户端入口后 `uv run python -m pytest -q conformance` 为 **8 passed**；`sh -n deploy/prepare-service.sh`、`git diff --check` 和 `uv build` 通过。测试通过只证明被覆盖的本地代码行为，不代表第 15 章所有 feature 已完成，也不代表线上部署。完整独立 Test Root CA selftest、ReadQuery/cursor、todo、Git 标准 push URL 与同域托管等仍待实现。

该批已提交为 `4338035`，其[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36277543483)已通过。后续文档与代码变更属于下一批，不能借用此 CI 结果。

## 2026-09-27 存储迁移与协议入口迭代

本轮本地验证使用 Python 3.15.0rc2、隔离 PostgreSQL 18 与临时 Valkey 9；没有替换生产数据库，也没有发布或线上验收结果。[远端 CI 运行](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36272190647)在提交 `c417b9e` 上通过，使用 PostgreSQL 16 与 Valkey 服务容器。

| 实际命令 | 结果 |
| --- | --- |
| `uv run --no-project --python 3.15 --with-editable . --with pytest --with pytest-asyncio pytest tests -q` | 155 passed in 92.29s，本批修改后全套重跑 |
| `uv run --no-project --python 3.15 --with-editable . --with pytest --with pytest-asyncio --with tiktoken pytest conformance -q` | 8 passed in 32.27s |
| `uv build` | GET-only 标量值上限 4096 字节修改后最新构建成功，生成 0.1.0a1 sdist 与 wheel |

远端 CI 同一提交上记录 **155 passed（164.99s）**、**8 passed（43.59s）**，并成功生成 sdist 与 wheel。第一次运行因 runner 的 `pg_dump` 比 PostgreSQL 18 服务容器旧，以及 RSS 首次分页依赖数据库排序规则而失败；修复提交 `c417b9e` 后全流程通过。
| `python3.15 -m compileall -q src` | 此前存储迁移批次成功 |

最终全套包括 PostgreSQL outbox、SQL 翻译、真实 Valkey 联动、协议路由、短码字典、AGENTS/真实 msg-entry 技能、/tools/ 与新建帖子/回复规范路径及授权后只读跳转、GET-only token/bootstrap 标量写和 1 KiB transfer.part_put 测试。此前 HTTP/字典/入口等定向检查已包含在全套中，不再次相加。

真实组件覆盖 PostgreSQL 事务回滚、并发写入、审计追加、持久 outbox、备份恢复，以及 Valkey 发布订阅和连接失败；Git 使用临时 bare 仓库，签名和客户端加密使用实际密码库。conformance 覆盖现有协议适配器的分片流程及 tokenizer 预算。本批已验证 /-/ 下 POST、签名 GET、MCP 路由与旧写入口拒绝，短码最小目录/分级详情、语义快照，以及新安装 /AGENTS.md、/.agents/skills/msg-entry/SKILL.md、/tools/（0500、tool.use）和新建 post/reply 的 .md 路径，以及授权后只读 308。

**这些通过结果不等于阶段 1 全部完成。** GET-only token/bootstrap 标量写已纳入本组全套；单业务值最多 4096 UTF-8 字节，默认原始 URL 路径上限仍为 8192 字节。当前 token 在原请求重放时可再次交付，不满足严格一次展示；没有本地随机材料的 bootstrap、复杂嵌套输入仍缺。308 仅将已有 .md Post 的旧式无后缀 URL 转到规范路径，私有资源先授权、不泄露目标；旧数据库中真正无后缀的存量 Post 没有自动改名或别名迁移。当前仅提供入口技能，不代表完整技能/局部规则发现已完成。短码仅覆盖顶层 enum/const，嵌套字段/preset 与完整 compact/normal/proof 投影仍待补齐。托管身份、分享和其他后续能力也不能据此称为完成。

仓库目前没有 `docs/verification/`、JUnit 汇总、构建日志或 package-smoke 文件；以上为本轮命令结果记录，不提供不存在的证据链接。早期“Python 3.13、101 + 5 用例、SQLite”的验收属于旧实现，不能作为本轮 PostgreSQL/Valkey 证据，也不能与本轮结果合并。

## 2026-09-27 ChatGPT 文件夹最新版需求对齐（本地与 CI）

重新从 Google Drive 的 `ChatGPT` 文件夹读取《msg.lmm.best｜项目设计》，本批依据其 2026-09-26T21:46:43.426Z 的 01–16 章版本。本批已提交为 `4c4b377`；本批[远端 CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36274644099)结果为 success，不再引用上表旧提交的 CI 作为本批证据。

| 实际命令 | 本批结果 |
| --- | --- |
| `uv run --no-project --python 3.15 --with-editable . --with pytest --with pytest-asyncio pytest tests -q` | 165 passed in 89.20s |
| `uv run --no-project --python 3.15 --with-editable . --with pytest --with pytest-asyncio --with tiktoken pytest conformance -q` | 8 passed in 31.47s |
| `uv build` | 0.1.0a1 sdist 与 wheel 构建成功 |
| `python3.15 -m compileall -q src`、`git diff --check` | 通过 |

提交 `4c4b377` 的远端环境为 Python 3.15、PostgreSQL 16 与 Valkey：测试 **165 passed in 175.58s**，conformance **8 passed in 44.22s**，sdist 与 wheel 构建成功。该结果证明本次提交的 CI 通过，不表示已发布、已部署或全部需求完成。

新增覆盖：旧 `/!`、`/~`、`/run/j|gz` 与 `/mcp` 执行别名移除；协议结构段拒绝编码别名和点段；非 `/-/` 的固定读取入口要求注册操作为 read。真实 `git-upload-pack` POST 返回 PACK，前后 refs 和关键业务表不变，`git-receive-pack` 被拒绝。`tool.run` 替代 `tool.invoke`，旧短码只保留 deprecated tombstone；`/-/transfer` 是复用六个现有 Transfer 操作的完整 OperationRequest 最小入口，`/-/d/<namespace>/<operation>` 增加分级详情。以上不证明完整 LFS、原始字节流入口或全部第 15 章 feature 验收。

## 尚未通过的验收

- 最新云盘需求与源码的差异见 [实现范围](IMPLEMENTATION_STATUS.md)，逐阶段出口见 [迭代计划](ITERATION_PLAN.md)。缺失功能不能靠现有测试数量抵消。
- 工具 runner fixture 不证明真实 bubblewrap 文件系统与网络隔离；需要真实公网、私网授权和重定向场景。
- SSH 命令解析、凭据和真实 Git hook 测试不证明 sshd 登录握手、禁止转发或宿主隔离。
- 邮件本地状态测试不证明真实 SMTP/TLS 投递；Webhook 尚无完整实现。
- 根管理内部测试不证明物理控制台身份与根目录权限；不能为测试关闭 local_only。
- 仍需要部署环境中的备份恢复演练及独立上线验收。本轮没有运行压力基准或独立安全审计，不声明吞吐量或安全覆盖率。

后续记录应同时包含代码提交、准确命令、环境版本、结果和可访问日志；区分单元/集成、协议 conformance、宿主验证及线上验证。disabled/skip 必须说明原因，不能记作 pass。正式功能必须在 CI 启用配置中执行。

第四批已完成合并定向：`.venv/bin/python -m pytest -q tests/test_share_grants.py tests/test_notes_todos.py tests/test_hosting_same_origin.py tests/test_sync_cursor.py tests/test_dictionary.py` 为 **28 passed**；短码snapshot由162增至173项，旧码意义不变。此前各项定向不再累加。第四批最终332/8/build通过，未提交、无对应CI。

ShareGrant为直接叶资源限时read/revoke/list；私有Note可单项分享但不授父目录列举。SOUL、Todo、DM、system-managed及preview均排除，不等于组分享/转授链/ShareLink。旧安装Online CA证书的grants是冻结快照，新增sharing.basic不能自动扩入旧证书；启用前需受控重签并验证当前授权范围，不能以新安装测试代替存量迁移。

第五批契约修正：system.maintenance@1保留原三种action不变，新增@2才包含deliver_due_todos；短码snapshot从173增至175，旧码含义保持，不在已发布@1中扩改枚举。Git上传新增120秒deadline，避免慢连接长期占据每worker两个slot；这只是当前未提交增量，当前本地336/8/build通过，仍未提交/无本批CI，不借用旧提交结果。

本批Domain Event Webhook已加独立webhook.domain capability，Basic OnlineIssuer普通issue_grants白名单不含该能力；订阅及每次投递复核owner/ACL与当前证书，无cap拒绝、证书撤销后停止投递。当前仅post_create/reply/post_edit，公网端到端仍未验。备份v4仅接v4、恢复drill写/worker硬闸及text_patch exact/context局部边界不变；392/8/build为未提交本地证据，无本批CI。
