# 项目设计变更记录与实施差距

三项DCR规范已写入[权威项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，不是待批准事项。本轮读取该文档修订`ANLCKQmHD9c-ju8OTbJ2QIem9CjzQKIpNwVNIxKrhZ47O54VGNYCNnqIaJ8czWILEXFuaBxzxZ8_A9oocGySQwmmVmTnXAubcqo05Xpa6o8`，共191个非空段。规范已生效不代表实现或整章验收完成。

## 当前实施状态（2026-09-27）

- 提交`135a190`已推送`main`。本地完整验证为541 passed、conformance 8 passed、隔离 build 成功；本地PG ledger定向检查9项通过。该提交的[GitHub CI 36318026014](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36318026014)已completed/success，日志为541 core passed、8 conformance passed、build成功。两者分别是本地与CI证据。
- 当前代码在executor幂等处理前拒绝旧`identity.temporary@1` token发行；batch拒绝`token_recover`、`custodial_create`等秘密子操作；URL中的秘密拒绝已有定向验证。以上是代码及本地验证结果。
- 反向代理日志尚未验证；旧的无钥临时主体迁移、历史密钥/备份退役与恢复后的撤销状态重放仍未完成，官方自动市场闭环也未验收。
- 生产尚未部署；CI成功不代表生产已验收。

[已完成部分](https://docs.google.com/document/d/1FtTdF5uhBPAsi-so-jOfpsiVI19RWKgx6bzIEFvpR2E/edit)已重新读取，修订`ANLCKQmjW4TG7EcyxL6DyAXC86ij36Ex01x5DzJ1NjrlYcJOn4GJcC_12tos9Q12O-Vv5NXH6SBjID-dAdVA13vnqhISDbYzDf_1QJXUKE4`，共71个非空段。原先链接漏写了`so-jOfpsi`中的连字符；现已更正。下文既有归档说明和历史CI记录继续保留作历史证据，不代表本文件讨论的秘密传输、恢复或密钥迁移整章已完成。

## DCR-01：秘密传输与纯路径边界——已采纳并扩充

正式规范已要求：token、recovery_secret、私钥及等价可重放凭据不得进入path/query/fragment、日志、Referer或错误回显。正式读取保留纯路径表达，但私有GET只能携短期绑定subject/query_digest/request_id/expires_at的proof，每次重验当前授权；复制URL在有效期内的重放窗口不能被隐藏。机密发行使用TLS body、接收者持钥绑定密文或明确安全通道；无能力时返回secure_channel_required，无秘密GET仍可用。

当前代码已拒绝URL中的秘密，相关定向验证通过；标准客户端也禁止秘密发行PathGET并要求真实域HTTPS。反向代理日志路径仍未验证，所有适配器/错误回显的拒绝矩阵、secure_channel_required统一行为、GET持钥客户端密文交付、敏感查询及URL重放边界仍需完成整体验收；不能仅凭局部拒绝测试称全平台符合。后续按已生效规范实现。

## DCR-02：一次释放与交付恢复——已采纳，15m成为默认规范

正式规范已把业务提交与秘密释放分离：响应前持久原子消费释放资格；同request_id只重放非秘密状态/receipt，明确token_delivery_unavailable或delivery_unavailable，不复制秘密到幂等存储。客户端发行前绑定并保存独立恢复材料，或已有可验证IdentityKey/RecoveryPolicy授权；默认15m恢复窗口，从业务提交起算，可配置但必须有确定上限，不因失败重试续期。恢复仅在原发行谱系原子换发并撤销前代，不能扩大权限/期限；恢复响应再次丢失仍需预绑定材料或绑定密文，过期走正式RecoveryPolicy。

提交`135a190`已包含executor在幂等处理前拒绝旧`identity.temporary@1` token发行，以及batch拒绝`token_recover`、`custodial_create`等秘密子操作；本地定向验证通过。[该提交的GitHub CI](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36318026014)已成功（541 core、8 conformance、build）。本地与CI结果分别记录，生产尚未部署。@2与客户端journal的一次领取/显式恢复、15m默认窗口与配置边界已有本地实现证据；恢复响应再次丢失、当前授权不扩大、其他入口完整性与持久化/日志拒绝矩阵仍待验收。旧版本存量迁移安排是实施工作，不重开已明确的默认值决定。

## DCR-03：历史密文与密钥退役——已采纳并扩展为两种恢复方式

正式规范要求冻结当前内容和全部保留历史清单；迁移期间相关新写入用新钥，清单变化产生增量并重验。不可变Revision的正文/签名/key_id不改，副本映射绑定旧Ref/Revision/key_id/digest与新密文/摘要/recipient。客户端逐项实际解密、校验完整性并签名ACK；ACK不证明未知/外部密文。

历史入口可以是A：新钥直接解的rewrap副本；B：只将该账号退役EncryptionSubkey包入新钥可解的RecoveryEnvelope，让客户端按旧key_id解原历史。B不得含IdentityKey、vault master key或其他主体钥，也不得让服务器重新持有可用旧钥；必须称兼容恢复，不能称历史重新加密。

状态须区分identity_switched/history_recoverable/server_key_retired，以及online_retired/backup_retired。无法迁移默认pending；用户可显式接受不可恢复，或保留受限旧钥并继续披露server-decryptable。在线销毁、备份退役、token撤销分别审计；恢复旧备份须重放撤销/退役状态，不复活token、证书当前授权或旧签名能力。

本批本地完整验证与PG ledger定向通过，以及[135a190的GitHub CI成功](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36318026014)，均不等于历史密钥迁移完成。旧无钥主体迁移、B类受限Envelope、新写入切换/增量、逐项解密闭环、状态拆分、备份退役及备份恢复后重放撤销/退役状态仍待实现或验收；finalize继续拒绝不完整迁移。生产未部署，不能将已采纳规范写成已交付。

## DCR-04：临时主体的双钥边界——用户已决定，本提交CI通过

现行设计要求建号同步生成独立IdentityKey与EncryptionSubkey。历史上的`identity.temporary@1/@2`只发行短期token，后续`identity.upgrade@2`才要求客户端双钥；当前工作树已拒绝这些旧版本的新建，其中`@1`发行在executor幂等处理前拒绝。历史存量临时主体仍可能没有双钥；当前提交CI验证了本批改动，但生产尚未部署或验收。

用户已选择保持现行“建号同步双钥”要求：临时创建也必须由客户端提供独立签名/加密公钥及签名持有证明，不设token-only例外。此前记录的本地505 core/8 conformance/build和`c919216` CI属于较早提交的验证证据。当前提交`135a190`另有[GitHub CI 36318026014成功](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36318026014)，日志为541 core passed、8 conformance passed、build成功；本地完整验证结果见顶部。生产尚未部署；旧临时主体迁移仍未验收，签名业务主体绑定/授权边界也仍待完整回归。

## 不因DCR采纳或条款迁出而减少的范围

货币、sale/bounty、确定性ClearingEngine、订单/BountyEscrow、Delivery与仲裁、官方market_e2e仍按现行设计验收。完整TUI、文件编辑、规则映射和宿主/生产恢复亦保留。已完成条款移到独立文档仅改变维护位置，不取消回归要求或放宽第15章完成门槛。
