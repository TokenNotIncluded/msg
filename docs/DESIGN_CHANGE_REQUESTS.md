# 项目设计变更记录与实施差距

三项DCR的主要建议已经写入[权威项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)，不再是“待用户批准”。本次实时读取迁移后修订 `ANLCKQmHD9c-ju8OTbJ2QIem9CjzQKIpNwVNIxKrhZ47O54VGNYCNnqIaJ8czWILEXFuaBxzxZ8_A9oocGySQwmmVmTnXAubcqo05Xpa6o8`，191个非空段。下文以该正文为规范，区分需求已生效与实现尚未完成。

[已完成部分](https://docs.google.com/document/d/1FtTdF5uhBPAsi-so-jOfpsiVI19RWKgx6bzIEFvpR2E/edit)当前修订`ANLCKQmjW4TG7Ecy…`、71段，已含A19/A20等后续归档；首次19段归档中的5个整段和4句依据e126539与成功CI36306888836，不能将后续全部归档归因于该次迁移；不表示本文件讨论的秘密传输/恢复完整功能已完成。cfd9a30远端CI36308410947为433/8/build成功；fb9bb65远端CI36310779270为502/8/build成功，3bc06ad另有成功CI36311726265（503/8/build）。当前未提交临时双钥@3本地505/8/build/diff通过、尚无本批CI；手动跨模块链属于fb9，但不是官方market_e2e。

此前189/71是历史基线。A01/A16/A18归档范围已修正：首次DM介绍@2已按3bc成功CI归档；临时主体双钥@3待本批CI与存量迁移验收，复杂GET短码及完整模板/示例仍在待办。这是完成状态纠偏，不撤销DCR三项已生效规范。

## DCR-01：秘密传输与纯路径边界——已采纳并扩充

正式规范已要求：token、recovery_secret、私钥及等价可重放凭据不得进入path/query/fragment、日志、Referer或错误回显。正式读取保留纯路径表达，但私有GET只能携短期绑定subject/query_digest/request_id/expires_at的proof，每次重验当前授权；复制URL在有效期内的重放窗口不能被隐藏。机密发行使用TLS body、接收者持钥绑定密文或明确安全通道；无能力时返回secure_channel_required，无秘密GET仍可用。

现有标准客户端已禁止秘密发行PathGET并要求真实域HTTPS，这是局部实现。遗留服务端token URL分支、所有适配器/错误/日志拒绝矩阵、secure_channel_required统一行为、GET持钥客户端密文交付、敏感查询及URL重放边界仍需补齐；不能仅凭客户端限制称全平台符合。后续按新规范实现，不再询问是否接受该原则。

## DCR-02：一次释放与交付恢复——已采纳，15m成为默认规范

正式规范已把业务提交与秘密释放分离：响应前持久原子消费释放资格；同request_id只重放非秘密状态/receipt，明确token_delivery_unavailable或delivery_unavailable，不复制秘密到幂等存储。客户端发行前绑定并保存独立恢复材料，或已有可验证IdentityKey/RecoveryPolicy授权；默认15m恢复窗口，从业务提交起算，可配置但必须有确定上限，不因失败重试续期。恢复仅在原发行谱系原子换发并撤销前代，不能扩大权限/期限；恢复响应再次丢失仍需预绑定材料或绑定密文，过期走正式RecoveryPolicy。

当前@2与客户端journal已有一次领取/显式恢复切片，旧@1重放秘密仍是合规缺口，不能继续以“建议未批准”为兼容理由。当前未提交切片已实现配置项`identity.credential_delivery_recovery_window`，默认15m、允许1–60m，恢复后代继承原deadline；相关定向26项通过，备份恢复保留该配置的切片已补，尚待本批整体回归。统一全入口行为、恢复再次丢失、当前授权不扩大与部署恢复完整矩阵仍需验收。旧版本迁移安排是实施工作，不重开已明确的默认值决定。

## DCR-03：历史密文与密钥退役——已采纳并扩展为两种恢复方式

正式规范要求冻结当前内容和全部保留历史清单；迁移期间相关新写入用新钥，清单变化产生增量并重验。不可变Revision的正文/签名/key_id不改，副本映射绑定旧Ref/Revision/key_id/digest与新密文/摘要/recipient。客户端逐项实际解密、校验完整性并签名ACK；ACK不证明未知/外部密文。

历史入口可以是A：新钥直接解的rewrap副本；B：只将该账号退役EncryptionSubkey包入新钥可解的RecoveryEnvelope，让客户端按旧key_id解原历史。B不得含IdentityKey、vault master key或其他主体钥，也不得让服务器重新持有可用旧钥；必须称兼容恢复，不能称历史重新加密。

状态须区分identity_switched/history_recoverable/server_key_retired，以及online_retired/backup_retired。无法迁移默认pending；用户可显式接受不可恢复，或保留受限旧钥并继续披露server-decryptable。在线销毁、备份退役、token撤销分别审计；恢复旧备份须重放撤销/退役状态，不复活token、证书当前授权或旧签名能力。

现有副本mapping、冻结清单与ACK不等于完整实现。B类受限Envelope、新写入切换/增量、逐项解密闭环、状态拆分、备份退役与旧备份防复活均仍有差距；finalize继续拒绝不完整迁移，不能把已采纳规范写成已经交付。

## DCR-04：临时主体的双钥边界——用户已决定，实施待CI

现行设计要求建号同步生成独立IdentityKey与EncryptionSubkey。此前`identity.temporary@1/@2`只发行短期token，后续`identity.upgrade@2`才要求客户端双钥；这些版本现已拒绝新建，但历史存量临时主体仍可能没有双钥。

用户已选择保持现行“建号同步双钥”要求：临时创建也必须由客户端提供独立签名/加密公钥及签名持有证明，不设token-only例外。当前工作树新增`identity.temporary@3`并让标准客户端预存双钥、绑定请求与恢复journal；旧`@1/@2`新建明确拒绝，已有旧临时主体仍可走双钥升级。双钥创建、丢响应恢复、旧入口拒绝及升级复用已纳入本地505 core/8 conformance/build验证；本批CI和旧数据迁移验收尚未完成，A01暂不扩成所有入口完成。

## 不因DCR采纳或条款迁出而减少的范围

货币、sale/bounty、确定性ClearingEngine、订单/BountyEscrow、Delivery与仲裁、官方market_e2e仍按现行设计验收。完整TUI、文件编辑、规则映射和宿主/生产恢复亦保留。已完成条款移到独立文档仅改变维护位置，不取消回归要求或放宽第15章完成门槛。
