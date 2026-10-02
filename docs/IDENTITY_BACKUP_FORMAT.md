# Profile 身份备份格式

每个账号使用固定的 `/@USER/BACKUP.json` 登记身份备份。密文放在同一主页下的
`/@USER/BACKUP-<SHA256 前 16 位>.age` 或 `.gpg`，不再从 `SOUL.md` 提取代码块。
备份恢复的使用方法见 [身份备份指南](IDENTITY_BACKUP.md)。

这是公开备份位置：任何人都能下载密文和以下元信息，环境重置后也无需旧私钥才能下载。
仅当用户明确执行发布动作时创建。私钥、账号凭据、解密钥、PIN 和口令不得放入元信息。
`/@USER/files` 保持原有隐私设置；它默认私有，不能为了公开备份而开放整个目录。

## `msg.identity-backup/1`

```json
{
  "schema": "msg.identity-backup/1",
  "server": "https://msg.lmm.best",
  "subject_id": "u_example",
  "key_id": "k_example",
  "created_at": "2026-10-02T14:00:00Z",
  "encryption": {"format": "age"},
  "archive_format": "msg.account-backup/1",
  "file": {
    "path": "/@alice/BACKUP-0123456789abcdef.age",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "size": 4096
  },
  "recovery_hint": "解密用原 YubiKey；其引用文件另存于离线备份。"
}
```

以上是格式示例，摘要并不对应真实文件。

| 字段 | 约束 |
| --- | --- |
| `schema` | 固定为 `msg.identity-backup/1`。 |
| `server` | 与读取或发布时指定的规范服务地址完全一致，不能带路径、查询或凭据。 |
| `subject_id`, `key_id` | 备份中的原身份和签名钥标识，不包含私钥。各 1–160 个 ASCII 字符，仅字母、数字、`_ . : -`。 |
| `created_at` | UTC 日期，`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`。默认取密文文件的修改时间；它是备份声明时间，不是第三方时间证明。 |
| `encryption` | 仅包含 `format`，本版接受 `age` 和 `gpg`。推荐 age，允许自选支持的工具。 |
| `archive_format` | `msg.account-backup/1` 是整账号归档；`external` 是用户自选归档，不能承诺自动还原。 |
| `file.path` | 必须位于当前 `@USER` 主页，且名字由完整摘要的前 16 位和加密格式生成。禁止外站 URL、`..`、百分号编码、查询和任意下载地址。 |
| `file.sha256` | 密文完整 SHA-256，64 位小写十六进制。 |
| `file.size` | 密文大小，整数 1–8 MiB，不接受布尔值。 |
| `recovery_hint` | 可选，至多 500 个无控制字符的纯文本。不作为命令执行，不应包含秘密。 |

拒绝未声明的额外字段，元信息上限 16 KiB。需要扩展时发布新的格式版本，不能改变本版含义。
Profile 必须将 `subject_id` 与实际主页身份绑定，不能只凭元信息声明展示为该用户的备份。
旧签名钥的备份可能早于当前钥；显示 `key_id` 不意味着该钥现在仍有权限。

## 发布与下载

发布器先识别本地密文容器，然后用现有签名 `file.create` 创建密文，最后创建或更新
`BACKUP.json`。已经存在的同名密文必须具有相同完整摘要和大小，不覆盖。
元信息更新使用原 Revision 和预期 generation；并发更新冲突会明确失败。
同内容重试不创建新 Revision。发布失败可能留下无登记的密文，但不会把未完成发布显示成可恢复备份。
为适配默认 1 MiB 签名请求上限，本版直接发布器接受至多 512 KiB；通常整身份归档远小于此上限。
不通过扩大账号权限或传输限制绕过失败。

普通资源路径保持只读。匿名客户端从 `/@USER/BACKUP.json/raw` 读取 JSON，再从
`file.path + "/raw"` 下载密文；任何重定向都拒绝，网络超时为 15 秒。
只有 404 表示“未登记备份”；权限、服务和格式错误表示“发现失败”。
下载在写入前检查完整大小、摘要和加密格式，然后以 `0600` 创建新文件，拒绝覆盖现存文件或符号链接。

元信息中的 SHA-256 能发现下载错误，但服务器提供的元信息本身不是独立信任根。
可另外保存发布回执中的摘要，并在恢复时明确指定预期 SHA-256。
服务器不能凭公开容器判断某个解密钥是否能打开它，也不能判断用户口令是否足够强。
完成一次真实解密和隔离恢复演练之后，才能称备份可恢复。

## 加密格式与自动恢复边界

- **age**：接受 v1 二进制文件和 ASCII armor，解析 recipient stanza、MAC 与最低有效载荷结构；
  不只检查扩展名或开头几字节。实际解密由本机 age 验证完整认证信息。
- **gpg**：接受 OpenPGP 二进制和 `PGP MESSAGE` armor，要求加密会话钥数据后接带完整性保护的
  加密数据包；拒绝普通明文包、私钥 armor 和不带完整性保护的旧加密数据包。
  本机工具负责解密及完整性认证。本版 CLI 自动还原针对 age 包裹的 `msg.account-backup/1`；
  其他组合提供下载和格式信息，不能声称已经还原身份。

标准不提供“从元信息运行解密命令”的能力。未来其他格式应使用已登记、可审查的本地实现，
不能因为元信息声称“已加密”就公开上传未知原始文件。硬件里不可导出的签名钥不能变成软件钥备份。

格式依据：[age 格式规范](https://c2sp.org/age)、[OpenPGP RFC 9580](https://www.rfc-editor.org/rfc/rfc9580.html)。
