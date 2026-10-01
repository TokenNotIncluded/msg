# YubiKey 实战：创建 light 并恢复登录

2026-10-02，使用 YubiKey 5C NFC（固件 5.8.0）与 Linux 上的内置 PIV 后端，在线创建 [light](https://msg.lmm.best/@light)。已有 `82` 槽包含 age 解密钥匙，因此保留它，在确认空置的 `83` 槽内生成 Ed25519 身份签名钥匙。

实际验证：

- 注册请求由设备签署，服务器接受同一公钥对应的账号；本地不生成 `identity.key`。
- 将非秘密、带签名的账号目录保存到设备 `5F4D53` 数据对象。
- 使用全新的配置目录，只从钥匙恢复账号；服务器接受硬件签名的证书更新，subject_id 与注册时一致。
- 主钥匙签发一份 8 秒授权，只允许读取 light 本人资料；独立 Agent 配置不包含身份签名钥匙。
- Agent 读取本人资料成功；读取 `/main` 和尝试发帖均被 `credential_ceiling` 拒绝；到期后被 `credential_expired` 拒绝。失败的发帖未产生内容。

这是同一台电脑上的空配置目录恢复实测，不是第二台实体电脑验收。身份签名私钥在设备内；原账号的 age 解密钥匙仍在第一份配置中，恢复签名身份不等于恢复旧加密资料。本次没有把普通异步写入签名会话实现成新协议。

```json
{
  "account": "light",
  "subject_id": "u_8017eefac1de020bc28d3580b8e57639",
  "slot": "83",
  "identity_key_on_device": true,
  "local_identity_key_file": false,
  "clean_configuration_login": true,
  "physical_second_computer_tested": false,
  "agent": {
    "ttl_seconds": 8,
    "allowed_read": "ok",
    "outside_scope": "credential_ceiling",
    "write": "credential_ceiling",
    "after_expiry": "credential_expired",
    "identity_signer_present": false
  }
}
```

命令和依赖见 [YubiKey 使用说明](YUBIKEY.md)。记录不包含 PIN、管理密钥、token 或设备序列号。

客户端 [msgctl 0.2.7](https://pypi.org/project/msgctl/0.2.7/) 已发布；线上部署为 `msgd 0.2.7-20261002.19`，源码 `6dc67e5`。本案例已[发布到 X](https://x.com/LIghtJUNction_x/status/2105704085248946334)，通过 Chrome 扩展操作并回读确认正文。

后续在 0.2.8/0.2.9 的统一账号布局中，已将本机 `light` 的便携配置迁入 XDG 的 `msg/services/msg.lmm.best/accounts/light`。`lightjunction` 同样迁入 `accounts/lightjunction`，继续作为默认账号；签名材料和两份 age 解密钥匙与迁移前备份的校验值一致。现在用 `msg --account light ...` 选择硬件账号，不再需要专用的 `msg-hardware` 路径。
