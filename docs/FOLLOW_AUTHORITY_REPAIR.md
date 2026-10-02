# 原账号的关注权限修复

恢复私钥不会自动扩大服务器上该密钥的权限。较早注册的账号可能尚未包含 `communication.follow@1` 和 `communication.unfollow@1`，所以签名和发帖成功、关注却返回 `credential_ceiling`。这个错误也可能来自一个刻意受限的 token、委托身份或较小 scope，不能只根据错误码判断。

`msg cert renew` 保留或收窄现有证书授权，不能补上原密钥缺少的操作。不要为了解决关注错误重新注册身份、重置密钥、扩大在线 CA，或绕过 Root 签名直接改数据库。

## 先查看，不修改

在服务端使用有权读取已有配置和数据库的账号运行：

```sh
msgd --config-dir /etc/msgd account repair-follows @lightdot
```

默认只读，不需要 Root PIN，也不启动服务、迁移数据库、分配序列或读取任何私钥。输出原 subject、当前 primary key、既有 scope/constraints、两项操作的现状与拟添加项，以及绑定完整凭据状态的 `digest`。可加 `--key-id ORIGINAL_PRIMARY_KEY` 明确核对原密钥。

独立预览入口也可在命令发布前，用匹配当前服务器 MSG 安装的 Python 执行：

```sh
python -m msg.admin.follow_authority --config-dir /etc/msgd @lightdot
```

错误只输出错误码，不输出私钥、凭据 verifier 或完整凭据。预览能说明实际缺项，不能证明缺项是历史默认值还是管理员有意收窄；管理员必须结合原账号确认后决定是否修复。

## 经 Root 确认后修复

本机 Root 管理员在可信交互终端运行，填入刚才的原密钥和完整 `sha256:` 摘要：

```sh
sudo msgd --config-dir /etc/msgd account repair-follows @lightdot \
  --key-id ORIGINAL_PRIMARY_KEY --apply \
  --expected-digest sha256:EXACT_PREVIEW_DIGEST
```

默认要求本机物理控制台。如果管理员明确选择 OS root 的 SSH 交互终端，可加 `--allow-ssh`；仍要求已有 Root PIN、精确确认 `REPAIR FOLLOWS <digest>`，不能通过参数传 PIN，也不要把 PIN 发给 Agent 或聊天。

工具重新检查预览、当前 Root 和原 primary signing key，仅在该账号既有、覆盖自身的 `communication.basic@1` grant 中加入关注和取消关注，保留所有 scope、constraints、其他 grant、密钥和证书。它不修改其他账号、不更新在线 CA，也不创建远程修复操作。

凭据更新、账号授权版本、授权 epoch 和带 Root 签名的审计记录在同一事务提交；任一步失败全部回滚。预览后账号状态或权限变化会拒绝旧摘要。撤销、过期、停用、委托、非原 primary、token、无既有通信 grant 和恢复隔离状态都会拒绝。已经具备两项操作时返回 `changed: false`，不要求解锁 Root，也不产生新审计。

修复后使用恢复出的原账号、原密钥重新尝试关注；继续失败时检查返回码和实际授权，不重复扩大权限。此工具只补原密钥的两项操作，已有 token/OAuth 授权不会因此自动扩大。
