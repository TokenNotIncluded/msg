# 内置 YubiKey 插件方案草案

状态：仅研究，尚未实现。用户已选择“主钥匙在 YubiKey，Agent 使用短期受限授权”。本次不生成、导入、覆盖或删除设备/电脑上的任何密钥。

## 推荐方向

内置客户端 signer 后端 `yubikey-piv`，默认随 msgctl 安装；发现设备后可用，未绑定设备时不改变原认证行为。优先支持带 PIV Ed25519 的 YubiKey 5 系列固件 5.7+。FIDO-only Security Key 不在此 PIV 方案范围内。PC/SC 系统组件仍是操作系统前提，安装器和 doctor 应清楚检查，不能把 Python 插件已安装等同于所有平台都能访问设备。

主身份签名私钥在设备内生成，优先绑定到现有账号的 `identity.key_add`，而非注册第二个账号。选用经检查为空的 PIV 签名槽，不固定覆盖常用 9A/9C。MSG 使用现有 Ed25519 验签，不因硬件类型自动增加管理员或 CA 权限。

目前 ClientState/save_signer 假定可取得 `private_bytes()`。研发时应拆分“签名接口”和“可导出软件密钥存储”：硬件仅提供 public_key、key_id、sign。已有硬件存根却未插设备时须明确失败，不能自动生成软件密钥或重新注册账号。

## 换电脑与存根

卡内使用一个 MSG 专属、经冲突检查的 PIV 数据对象，存储带版本、长度界限和签名的小型账号目录：服务 origin、账号 subject_id、签名槽位、算法、公钥/指纹、可选账号显示名。它是发现线索，不是登录凭据，也不能修改服务端授权。undefined PIV 对象通常无需 PIN 即可读取，所以这里只放可公开的信息，不放 token、PIN、私钥或管理员管理密钥。

新电脑：安装 msgctl → 插入设备 → 读取目录及对应公钥 → 确认服务地址/首次信任 → 本地安全输入 PIN 并按策略触摸 → 设备签署现有 MSG 登录/授权请求 → 服务端核对账号、密钥、撤销状态及当前权限 → 自动生成本机存根。目录签名与设备公钥必须一致；设备序列号只作为查找提示，身份由公钥指纹和签名确认。用户显式填写服务地址可作为目录丢失时的备用发现入口。

本机存根示意（拟定格式）：

```json
{
  "version": 1,
  "signer": {
    "backend": "yubikey-piv",
    "slot": "82",
    "algorithm": "ed25519",
    "key_id": "k_...",
    "public_key": "..."
  },
  "profiles": [{"server": "https://msg.example", "subject_id": "u_..."}]
}
```

存根是可重建的硬件引用，目录 0700、文件 0600；删除电脑配置不会删除设备密钥。证书、服务标识与用户名只是缓存，联网后重新验证，账号改名不改变 subject_id 或 key_id。可能存在的短期 token 属于另一份受保护的会话状态，不与公开存根混写，不复制到其他电脑。

## 硬件签名长度和异步 Agent

Yubico 的 PIV Ed25519 实现把原始 message 交给设备，PC/SC APDU 有长度限制；当前 Yubico Python SDK 对新固件的最大 APDU 约 3062 字节，实际可用 message 还要扣去编码开销。MSG 当前签署带领域前缀的完整规范请求，不能假定长帖子/批次可直接送入设备，也不能擅自先 hash 再冒充现有 Ed25519 协议。

第一阶段建议硬件主钥匙签短小的登录/授权请求；异步 Agent 使用现有短期、范围受限凭据，软件临时签名密钥仅用于确有签名要求的操作。主身份私钥仍不离开设备。授权只给明确服务、操作及 Agent 私有路径；使用独立会话存储和生命周期。完整请求硬件签署只支持验证过的长度，超限明确失败，后续若需统一硬件签署大请求必须单独讨论版本化协议。

插入钥匙不是自动放行：PIN/触摸在本机完成，不进日志、聊天或命令行参数。浏览器先沿用 CLI 批准流程；直接从浏览器访问 PIV 需另行设计本机桥接。拔出设备会阻止新的硬件签名，但不会自动撤销已经授出的浏览器会话或远程短期凭据。

## 恢复和迁移

优先在钥匙内新生成密钥并给同一账号增加硬件 key，保留原账号和权限；不强制导入原私钥。现有 `identity.key_add/key_revoke` 会更新 auth_version，旧会话可能需要重新登录，应在迁移界面说明。删除旧软件主钥匙必须等设备签名、全新电脑登录以及第二条恢复途径都验证后，由用户明确决定。

备用 YubiKey 应生成另一把独立密钥并绑定同一账号。设备内生成的不可导出密钥不能事后复制到备用钥匙；账号恢复依靠预先授权的第二把钥匙或明确的恢复流程。保留加密密钥的独立设计：Ed25519 签名钥匙不等于 X25519/age 解密钥匙，签名登录成功不代表旧加密资料能在新机解密。

## 第一阶段验收

1. 软件签名接口改造不破坏现有身份和 keystore；硬件后端不暴露 private_bytes，不自动降级。
2. 使用空槽的真实设备验证生成、公钥读取、MSG 原始帧验签、PIN/触摸/超时以及消息长度边界。
3. 空配置目录的新电脑仅靠钥匙目录和本地授权恢复同一账号；篡改目录、错钥匙、已撤销密钥均拒绝。
4. 桌面 Linux/Windows/macOS 分别确认 PC/SC、设备锁和热插拔；与现有 OATH/OpenPGP 应用共存。
5. 主/子 Agent 凭据边界、撤销、短期授权到期以及浏览器登录各自验证，不混称为一项“登录成功”。

## 官方依据

- PIV Ed25519 与固件要求：https://docs.yubico.com/hardware/yubikey/yk-tech-manual/yk5-firmware-5.7.html
- 设备密钥生成/导入：https://docs.yubico.com/yesdk/users-manual/application-piv/private-keys.html
- 专属 PIV 数据对象及读取限制：https://docs.yubico.com/yesdk/users-manual/application-piv/get-and-put-data.html
- PIV Ed25519 原始消息处理：https://github.com/Yubico/yubikey-manager/blob/main/yubikit/piv.py
- PC/SC APDU 长度约束：https://github.com/Yubico/yubikey-manager/blob/main/yubikit/core/smartcard/__init__.py
