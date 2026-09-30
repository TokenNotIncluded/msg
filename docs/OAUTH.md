# msgctl OAuth 登录与 API key

MSG 可以作为 OAuth 2.0 / OpenID Connect 提供方。身份仍由现有 Ed25519 私钥或托管 vault 控制；OAuth 授权不会创建第二套账号、证书或业务权限。

## 启用和注册应用

在 `msgd.toml` 加入：

```toml
[oauth]
enabled = true
access_ttl = 900
session_ttl = 2592000

[[oauth.clients]]
client_id = "my-app"
name = "My app"
redirect_uris = ["https://app.example.com/auth/msg/callback"]
scopes = ["openid", "profile", "offline_access", "msg.read"]
```

`msg-cli` 是内置设备码客户端，不接受浏览器回调。其他客户端需要明确登记；没有动态注册、通配回调、密码授权或 implicit grant。当前客户端均为 public client，`token_endpoint_auth_method=none`；服务器应用也必须发送 PKCE。

生产地址必须使用 HTTPS。回调地址精确匹配登记值，不允许 query、fragment、用户名或密码；本机开发仅允许明确登记的 `http://127.0.0.1` / `http://[::1]` 地址。

启动时数据库会创建 `oauth_states` 表和到期索引。独立托管读取进程也需要查询凭据来源及会话撤销状态；**现有**读取角色需由数据库所有者在升级后执行：

```sql
GRANT SELECT ON public.oauth_states TO msgd_hosting;
```

新的角色使用更新后的 `deploy/hosting-role.sql`。没有该权限时读取进程拒绝启动，不会自行扩大数据库权限。重启服务和读取进程后配置生效。

## 浏览器

访问 `/oauth/authorize`，发送 `response_type=code`、登记的 `client_id` 和 `redirect_uri`、`scope`、随机 `state`、`code_challenge`、`code_challenge_method=S256`。登录用途应发送随机 `nonce`。客户端必须验证回调 `state` 和 ID token 的签名、`iss`、`aud`、`exp`、`nonce`；以 `sub` 识别用户，不以可变名称识别用户。

第一次登录打开 `/oauth/login`，页面显示 `msg auth approve XXXXXXXX`。在持有身份私钥的 CLI 中执行即可确认浏览器登录。`msg auth request XXXXXXXX` 可先查看应用和权限，`msg auth deny XXXXXXXX` 拒绝请求。

没有本地私钥的用户可以访问 `/oauth/signup`，沿用现有托管 key 注册流程。服务器保管该身份的签名和加密密钥，登录会话绑定到活跃 vault；原有托管升级流程仍可将身份转为自行保管。托管会话不要求每小时重新使用 bootstrap token。浏览器登录态是 HttpOnly、SameSite=Lax cookie，HTTPS 下使用 Secure 和 `__Host-` 名称。网页没有把 access/refresh token 写入 localStorage。

登录后授权页面展示应用、当前身份和请求权限，点击同意才签发一次性授权码。授权码仅存活 60 秒。客户端后端向 `/oauth/token` POST 表单兑换，必须提供同一 `client_id`、`redirect_uri` 和 PKCE `code_verifier`。现阶段跨域网站应由其后端兑换；端点没有开放跨域浏览器 CORS。

浏览器会话默认有效 30 天，绑定原身份密钥、身份授权版本、托管状态和恢复状态。会话撤销后，由它授出的 OAuth token 同时失效。首次申请托管身份后，应通过设备码在一个可信 CLI 中保存登录态；丢失所有登录凭据时不能凭名称取回托管身份。

## CLI / 有浏览器的 Agent

```bash
msg login
msg auth status
msg read /main
msg logout
```

`msg login` 尝试打开浏览器，也在 stderr 显示验证地址和设备码。设备没有浏览器、处于 SSH / 容器内时：

```bash
msg login --no-browser
```

用手机或另一台电脑打开显示的地址、输入设备码、登录并同意。CLI 在规定间隔内轮询，保存凭据后退出。验证码本身不能登录、不能批准权限；可使用已登录的私钥 CLI 直接 `msg auth approve CODE`。

凭据保存为配置目录中的 `oauth-session.json`，权限为 0600；不写入正常 status 输出。CLI 重启后继续使用，access token 到期前自动刷新。刷新 token 每次轮换，绝对到期时间不延长；再次使用旧刷新 token 会撤销整个授权。多个 CLI 进程通过现有身份状态锁串行刷新。若刷新响应丢失，客户端保存未完成标志并要求重新登录，避免盲目重试已经消耗的刷新 token。私钥不会因此被删除。

access token 默认 15 分钟，长期凭据默认 30 天。原来源凭据有更早到期时间时采用较早值。CLI 一次配置目录对应一个服务和身份；使用不同 `--config-dir` 隔离多个身份。

## API key / 受限环境

持有私钥的身份可以申请日常 API key：

```bash
msg api-key create --ttl 86400
msg api-key show
msg api-key rotate --ttl 86400
msg api-key revoke
```

创建和轮换必须验证私钥签名。默认 ceiling 仅包含该身份当前具备的普通读取操作；通过 `--ceiling '[...]'` 传入现有 CapabilityGrant 数组可明确缩小或指定权限，但不能超过签名凭据的权限。API key 的最长有效期为 24 小时。

首次创建/轮换命令仅一次输出 key，并将其保存供后续 CLI 调用。`show` 只显示标识和到期时间。轮换在一次事务里创建新 key 并撤销旧 key。来源签名凭据撤销、到期或授权版本改变后，新 key 也会失效。key 不具有签发其他 key 的权力。

申请复用 `identity.token_create@3`、既有一次性秘密交付和独立 recovery secret。丢失交付响应时保留 0600 的 `api-key-create.json`，执行 `msg identity recover-token` 恢复；私钥不变，来源绑定也不会丢失。

API key / OAuth access token 采用 `credential_id.base64url_secret` 形式，允许放在 POST `/-/p/<operation>` 的 `Authorization: Bearer ...` 请求头。业务请求仍使用现有 OperationRequest，subject 必须等于 token 身份，proof 留空；服务端注入 TokenProof 后经过相同 executor、权限、签名要求和恢复门槛。也可直接使用原有 TokenProof。不能同时提供 Bearer 和另一份 proof。

只能通过受限 GET / 浏览器交互的 Agent 继续使用原签名路径；不允许把 API key、access token、refresh token 放进 URL。PathGETTransport 保留原有秘密通道拒绝规则。私钥仍用于需要签名的内容确认、密钥/授权/资金等操作，OAuth token 不会替代这些要求。

## 端点和权限

| 端点 | 用途 |
| --- | --- |
| `/.well-known/oauth-authorization-server` | OAuth 元数据 |
| `/.well-known/openid-configuration` | OIDC 元数据 |
| `/oauth/authorize` | 浏览器授权码 + PKCE |
| `/oauth/device_authorization` | 设备码申请 |
| `/oauth/device` | 设备授权确认 |
| `/oauth/token` | 授权码、设备码兑换 / 刷新 |
| `/oauth/userinfo` | `sub` 和可选 `preferred_username` |
| `/oauth/jwks` | 验证 ID token 的公开密钥 |
| `/oauth/revoke` | 撤销所属客户端的授权 |
| `/oauth/logout` | 浏览器会话退出，需 Origin + CSRF |

| scope | 含义 |
| --- | --- |
| `openid` | 身份登录，返回签名 ID token，可访问 userinfo |
| `profile` | userinfo 返回当前名称 |
| `offline_access` | 请求逐次轮换的刷新 token |
| `msg.read` | 普通读取操作的凭据上限 |
| `msg.write` | 普通写操作的凭据上限；仍受签名和现有业务权限约束 |

`scope` 是权限上限，不是额外授权。没有 email claim、动态客户端注册、外部身份提供商登录、跨域 CORS 或完整 OIDC 认证声明。

所有随机 token、cookie、授权码只存摘要；权限和来源信息保存在 PostgreSQL。认证记录与业务提交共享已有写锁，重复消费和并发刷新不能产生两个有效后继。登录端点带请求上限和到期清理，拒绝跨站 cookie 写入；HTTP 缓存关闭，浏览器引用来源隐藏。恢复隔离、过期运行 generation、来源密钥撤销或来源记录缺失均拒绝访问。

参考：[OAuth 安全建议 RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html)、[设备码 RFC 8628](https://www.rfc-editor.org/rfc/rfc8628.html)、[OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html)。

## 当前权限与只读信息

非托管 OAuth 与 API key 的权限上限还须落在来源密钥当前的 operations、scope 和
constraints 内；保持同一 key ID 或 auth_version 不会保留来源已失去的权限。
托管 vault 的签名 key 本身没有请求 grants，已批准会话按捕获的上限与当前临时策略
双重限制，同时检查活跃 vault、撤销、身份版本、会话及恢复状态；bootstrap token 的
一小时自然到期不缩短已批准会话。策略放宽不会扩大既有会话捕获的上限。
`GET` 和 `POST /oauth/userinfo` 使用数据库只读事务，不写持久 rate 状态或产生外发
副作用；仍检查当前来源、family、runtime generation 与 quarantine。
