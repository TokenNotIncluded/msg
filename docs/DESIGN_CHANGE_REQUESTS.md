# 设计变更契约与实现缺口

以下契约来自[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)。本文件记录技术边界和仍需验收的部分；具体测试结果归属于运行测试时的提交。

## DCR-01：秘密传输与纯路径读取

token、recovery_secret、私钥及等价可重放凭据不得进入 URL 的 path、query、fragment，也不得进入日志、Referer 或错误回显。正式读取保留纯路径表达；私有 GET 的 proof 绑定 subject、query_digest、request_id 和 expires_at，并在每次读取时重新检查当前授权。复制签名 URL 在有效期内仍可重放。

机密发行使用 TLS 请求体、接收者持钥绑定密文或明确的安全通道；没有安全通道时返回 `secure_channel_required`。不含秘密的 GET 仍可使用。

当前实现已有 URL 秘密拒绝，标准客户端也禁止通过 PathGET 发行秘密并要求真实域 HTTPS。仍需验收所有适配器与错误回显、反向代理和访问日志、接收者绑定密文交付、敏感查询保密以及 URL 重放边界。

## DCR-02：一次释放与交付恢复

业务提交和秘密释放分离：发送响应前持久、原子地消费释放资格。同一 request_id 只重放不含秘密的状态或 receipt；释放不可用时返回 `token_delivery_unavailable` 或 `delivery_unavailable`，不把秘密写入幂等存储。

客户端发行前保存独立恢复材料，或使用已有可验证的 IdentityKey、RecoveryPolicy 授权。默认恢复窗口为 15 分钟，从业务提交起算；配置必须有确定上限，失败重试不续期。恢复仅在原发行谱系中原子换发并撤销前代，不扩大权限或期限。恢复响应再次丢失时仍需预绑定材料或绑定密文；过期后走正式 RecoveryPolicy。

executor 在幂等读取前拒绝旧版秘密发行；batch 拒绝 `identity.token_recover`、`identity.custodial_create` 等返回秘密的子操作。当前操作版本、客户端 journal、恢复响应再次丢失及当前授权拒绝矩阵见[凭据交付](CREDENTIAL_DELIVERY.md)。这些源码和隔离测试依据不代替生产反向代理、访问日志及接收者绑定密文交付的独立验收。

## DCR-03：历史密文与密钥退役

迁移需要冻结当前内容和全部保留历史清单；迁移期间的新写入使用新钥，清单变化产生增量并重新验证。不可变 Revision 的正文、签名和 key_id 不改。副本映射绑定旧 Ref、Revision、key_id、digest 与新密文、摘要和 recipient。客户端逐项解密、校验完整性并签名 ACK；ACK 不证明未知或外部密文可恢复。

历史恢复有两种入口：A 是新钥直接解密的 rewrap 副本；B 是仅将该账号退役 EncryptionSubkey 包入新钥可解的 RecoveryEnvelope，由客户端按旧 key_id 解原历史。B 不得包含 IdentityKey、vault master key 或其他主体的钥匙，也不得让服务器重新持有可用旧钥。B 属于兼容恢复，不能称为历史重新加密。

状态分别表示 `identity_switched`、`history_recoverable`、`server_key_retired`、`online_retired` 和 `backup_retired`。无法迁移时默认 pending；用户可显式接受不可恢复，或保留受限旧钥并继续披露 server-decryptable。在线销毁、备份退役和 token 撤销分别审计；恢复旧备份时须重放撤销及退役状态，不能复活 token、证书当前授权或旧签名能力。

旧无钥主体迁移、受限 Envelope、新写入切换及增量、逐项解密闭环、备份退役和恢复后重放仍需完整验收；`finalize` 对不完整迁移保持拒绝。

## DCR-04：临时主体的双钥边界

临时主体创建时，客户端提供独立的 IdentityKey 签名公钥和 EncryptionSubkey 加密公钥，并证明持有签名私钥。不设置 token-only 的新建例外；历史 `identity.temporary@1/@2` 新建已拒绝。

历史存量临时主体可能没有双钥，其升级、签名业务主体绑定、凭据上限和恢复矩阵仍需验收。

## DCR-05：知识衰减与时效声明

新增需求：内容和知识会过时，读取、搜索、推荐和 Agent 引用需要表达当前适用性。详细提案见[知识衰减设计](KNOWLEDGE_DECAY.md)，当前尚未实现；这是本地新增需求，不表示已经同步到上方云端项目设计。

时效声明绑定精确 Revision、digest 和适用范围。区分「未验证/待复核」「明确失效/已被替代」及读者异议，不因年龄自动判错或删除内容。阅读、点赞、普通回复和无证据的证明不能刷新验证时间；再验证需提供步骤、环境和结果，新 Revision 不继承旧验证。

声明与证据保留认证来源及历史，遵守当前权限和恢复边界；搜索、推荐及 Agent 引用携带相同状态与计算时点。到期提示、声明权限、版本隔离、替代范围、私有证据不泄露及恢复后不续期均需独立验收。

## 相关验收范围

货币、sale/bounty、ClearingEngine、订单与 BountyEscrow、Delivery、仲裁和官方 `market_e2e` 依照项目设计分别验收。TUI、文件编辑、规则映射以及宿主与生产恢复也需要各自的实现和验证；本文件所列局部实现不代表这些范围已经完成。
