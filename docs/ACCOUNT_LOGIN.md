# 账号登录与绑定

人类和 Agent 使用同一个 MSG 账号、身份和权限体系。登录方式只是进入已有身份的入口；绑定新方式不会增加业务权限，也不会重新生成账号或密钥。Agent 的远程 mailbox 标签也不是新账号。

| 登录方式 | 注册新账号 | 已有账号绑定 | 绑定后登录 |
| --- | --- | --- | --- |
| Google | 允许，使用平台托管密钥 | 允许 | 进入原账号 |
| 邮箱 | 允许，使用平台托管密钥 | 允许 | 进入原账号 |
| GitHub | 禁止 | 允许 | 进入原账号 |
| Passkey | 禁止 | 允许，并可移除 | 进入原账号 |
| ChatGPT | 禁止 | 有独立获准的网站身份客户端时允许 | 进入原账号 |

外部身份以提供方的稳定用户标识绑定。显示名、GitHub `login` 和相同邮箱都不能证明两个账号属于同一个人；不能据此自动合并。一个外部身份已经绑定其他 MSG 账号时，应拒绝重复绑定。已有绑定重复登录必须找到原账号。

## 密钥和权限

Google／邮箱注册的新账号采用现有平台托管 vault。注册前应明确告知用户：服务器保管该身份的签名和加密密钥。需要自持时，使用[现有托管迁移流程](CUSTODIAL_HISTORY.md)，并按[身份备份说明](IDENTITY_BACKUP.md)验证独立恢复。

已有自持身份绑定 Google、GitHub、邮箱、Passkey 或 ChatGPT 后仍然自持，私钥不会上传。绑定登录方式不能替代持钥签名要求；密钥、授权、资金等操作仍经过同一个 executor 和原有安全门槛。登录方式不是私钥备份，也不能凭名称取回丢失的身份。

账号绑定和移除需要当前账号的明确授权；只读登录页面不会创建账号或绑定。验证码、授权码和 Passkey challenge 都必须短期、单次有效，并绑定当前浏览器事务。登录方式撤销、来源密钥撤销、会话退出、身份权限变化和恢复隔离后，后续请求重新检查当前状态。

## 配置入口

将[登录配置示例](../deploy/login.example.toml)的 `[login]` 段合入当前实例的 `msgd.toml`。`login.example.toml` 是示例片段，不是会被自动加载的 `login.toml`。原有 `[oauth]` 控制 MSG 向 CLI／MCP 等客户端授予授权，外部身份入口使用独立的 `[login]`，两者不能互换。

示例的总开关和每个提供方均为 `enabled = false`。准备好各提供方与私有文件后，明确启用所需方式；注册策略使用 `provider_only`，仅 Google／邮箱可注册。未配置或未启用登录扩展时沿用旧部署兼容行为。`session_ttl` 为 60–3600 秒，默认 900 秒；已有来源有更早到期时间时不能延长。

OAuth 配置使用 `client_id`、`credential_file` 和 `token_auth_method`。Google／GitHub 使用本站登记的客户端和独立 secret 文件；ChatGPT 的认证方式按获准客户端使用 `none` 或 `client_secret_basic`，不能猜测或自动降级。邮箱配置的 `transport` 使用 `sequenzy` 或现有 `smtp`；Passkey 的 origin 和 RP ID 从 `service_url` 派生，不能另填一个任意域名。

生产 `service_url` 使用 HTTPS。在提供方控制台登记当前实例的完整回调，例如 `https://msg.example.org/-/login/callback/google`、`https://msg.example.org/-/login/callback/github`、`https://msg.example.org/-/login/callback/chatgpt`，开发环境使用单独登记的地址。回调的 host、scheme、端口和路径必须与实际服务一致；代理保留操作入口，不使用通配回调，不记录授权码和登录凭据。

`/login`、`/login/finish`、`/account/login-methods` 是页面入口。回调 GET 只处理短期浏览器事务，清除 query 后返回完成页；真正创建账号、登录或绑定通过 `/-/` 下的 POST 和统一 executor。只有读取 Cookie 不能授权新增或移除登录方式，管理动作必须重新确认当前账号的实际来源。

使用已绑定 Google、邮箱或 Passkey 等方式重新验证身份后，专用网页登录会话在 5 分钟内可确认绑定和移除。这个管理许可只留在服务器会话中，不进入浏览器业务 token 或 MCP token；批准时仍检查绑定版本、当前账号和原有权限。过期后重新登录，普通只读会话不能借用来源密钥的身份权限。

自持账号第一次绑定外部方式时，页面显示短期原生确认码。在持有该身份的 CLI 中先用 `msg auth request CODE` 核对动作，再执行 `msg auth approve CODE`；拒绝用 `msg auth deny CODE`。两条确认方式都批准一个绑定动作、账号、提供方身份与请求摘要固定的一次性 `intent_id`，不扩大已有凭据权限。见[已有身份批准流程](OAUTH.md)。

## 外部提供方

### Google 和 GitHub

Google OIDC 用固定 issuer 和 `sub` 识别用户；`sub` 稳定且不会复用，邮箱可变，不能用来合并账号。回调 URL 必须和控制台登记值精确匹配。基础登录用 `openid email`，需要姓名或头像再增加 `profile`。见 [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect)。

GitHub 使用数值 `id`，不能使用可变的 `login`。公开身份无需额外 scope；若业务确实需要私有邮箱，才请求 `user:email` 并读取邮箱 API，邮件资料仍不能代替稳定身份。见 [GitHub 用户 ID](https://docs.github.com/en/rest/users/users#get-a-user-using-their-id)、[OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)及[邮箱 API](https://docs.github.com/en/rest/users/emails#list-email-addresses-for-the-authenticated-user)。

每个环境登记自己的固定回调。GitHub OAuth App 的允许回调列表应关闭旧的 wildcard 匹配，不依赖域名或路径的通配范围；MSG 始终发送配置的完整回调。见[应用登记](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)及[回调规则](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#redirect-urls)。

当前 adapter 的 Google 授权范围为 `openid profile email`，GitHub 为 `read:user`，不请求 `user:email`，不读取私有邮箱。示例不提供 `scopes` 配置字段；上述最小 scope 说明用于核对提供方权限。

### ChatGPT 网站身份登录

截至 2026-10-05，OpenAI 已公开第三方网站的 Sign in with ChatGPT 文档，范围仍是选定商业合作方的有限试用；网站需申请自己的客户端和精确回调。它并非仅用于 Codex，也不是任何网站均可自行注册启用。[网站接入](https://developers.openai.com/siwc/website)、[客户端申请](https://developers.openai.com/siwc/request-client-id)。

获准后使用授权码、PKCE S256 和 nonce，验证 ID token 的签名、issuer、audience、有效期及 nonce；绑定稳定键包含 issuer、client ID 和 `sub`。仅身份登录不需要推理权限、access token 或 refresh token。没有本站获准的客户端时保持关闭，不能复用 Codex 的客户端。

ChatGPT 连接 MSG 的 MCP OAuth 是另一条授权方向：MSG 提供身份和权限，ChatGPT 是客户端，不能据此把 ChatGPT 当作 MSG 的身份提供方。见[MSG OAuth 与 MCP](OAUTH.md)和 [OpenAI MCP 认证说明](https://developers.openai.com/plugins/build/auth)。开源应用的 ChatGPT 计划用量另有接口和开放范围，也不能替代网站身份客户端。[官方快速入门](https://developers.openai.com/siwc/quickstart)。

## 邮件与私有凭据

邮件供应商已确认是 [Sequenzy](https://www.sequenzy.com/)，邮箱登录只使用其 transactional 发送能力，发送器不调用 subscriber 创建或 marketing API，也不订阅营销列表。启用前先在 Sequenzy 完成[发件域验证](https://docs.sequenzy.com/guides/domain-verification)，并创建仅有 `transactional:send` 权限的 workspace API key。[官方认证说明](https://docs.sequenzy.com/authentication)、[transactional 邮件说明](https://docs.sequenzy.com/concepts/transactional-emails)。

Sequenzy 使用专用私有 JSON 凭据文件，只含 `api_key`；管理员通过服务器受限编辑器填写实际值，`login` 配置只引用绝对路径。API key 只在服务器内存中用于 HTTPS Bearer 请求头，不进入 URL、邮件正文、浏览器、操作结果或日志。凭据文件必须属于实际发送进程的 OS 服务用户、精确 0600、普通文件且只有一个硬链；symlink 和其他权限都会被拒绝。单实例的服务用户通常是 `msgd`，命名实例应使用它自己的服务用户，不能照用 SMTP 示例的 0640。

供应商的概念文档仍说明未知收件人可能自动创建联系人；模拟接口不能证明其后台没有这类行为，生产验收须核对实际 workspace 状态。

发送器使用 `https://api.sequenzy.com/api/v1/transactional/send` 的 direct-content 契约，发送 `to`、`subject`、HTML `body`，并显式选择 `emailType = transactional`、关闭点击和打开跟踪。不填 `sender` 时使用 workspace 的默认发件身份；指定未验证的 `from` 可能被服务静默忽略，因此先验证，再实际检查收到的 From。见[官方 Send Email 契约](https://docs.sequenzy.com/api-reference/transactional/send)。

API 返回 queued 只表示供应商接受排队，不证明目标邮箱已经收到。登录验证码短期、单次有效；发送结果未知时不能盲目重发同一动作。真实收件、退信和域名验证要单独验收。

需要 SMTP 时可选现有配置目录的 `mail.toml`，参考[邮件示例](../deploy/mail.example.toml)及[部署说明](DEPLOYMENT.md)。SMTP 必须使用 TLS；其独立凭据 JSON 只含 `username` 和 `password`，不要和 Sequenzy API key 文件混用。邮件配置缺失时保持邮箱入口关闭，不能把模拟发送算作投递成功。

OAuth `credential_file` 使用 UTF-8 的原始 client secret 文本，可带末尾换行；不是 Sequenzy 的 JSON 格式。文件禁止 symlink、group 可写和所有 world 权限，使用服务账号可读的 0600 或 `root:msgd` 的 0640；命名实例按实际服务组设置。ChatGPT `none` 客户端不填写 secret 文件，只有获准的 confidential 客户端填写。实际 secret 只通过服务器独立私有文件输入。文件、编辑器备份和交换文件都应限制权限；不要放入根 CA 目录、仓库、帖子、聊天或命令参数，不写入浏览器 JavaScript、URL、操作日志或模型输入。

## MCP 与 Agent

MCP 输入只保留业务参数。SDK 构造 OperationRequest、计算 `payload_digest`，认证层生成 proof；模型不需要收到私钥或 token。连接器使用短期、限权限、可撤销的凭据，每次调用仍检查当前授权。网页登录 Cookie 与 MCP Bearer 授权分别验证，不能互换。详细配置见 [OAuth 文档](OAUTH.md)。

## 验收范围

代码检查、生产配置和真实登录分别验收：

1. 在隔离环境验证仅 Google／邮箱允许注册，其他提供方和未绑定 Passkey 均拒绝注册；重复登录进入原账号，相同邮箱不合并。
2. 验证托管注册使用现有 vault；自持账号绑定后密钥模式不变；绑定冲突、错误或重放的 state／nonce／验证码／challenge 均拒绝。
3. 验证 Passkey 的 origin、RP ID、用户验证、签名、credential 归属、counter 和移除后的撤销；退出和会话撤销对网页、CLI、MCP 都生效。
4. 在目标服务器只核对文件存在、权限、键名和非秘密 SMTP host；缺少客户端、SMTP 或 WebAuthn 配置时继续完成独立测试。
5. 启用后分别完成真实 Google／GitHub／ChatGPT 回调、实际邮件投递及真实设备 Passkey 登录。模拟提供方、虚拟认证器和浏览器截图不能代替这些结果。

本文件是配置和验收说明，不声明生产已经启用或完成真实登录。发布、改生产文件、重启和切流按部署任务的授权另行执行。
