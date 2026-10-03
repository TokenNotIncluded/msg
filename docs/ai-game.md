# AI 驾驶与游戏 webhook

先用 `pilot --guest` 匿名驾驶；它不读取账号状态、私钥或证书，也不注册账号。用账号身份驾驶和订阅自己的 webhook，需要先取得对应操作的有效授权。示例见 [game_bot.py](../examples/game_bot.py)，游戏规则见 [联机飞船](LIVE_FLIGHT.md)。

本文对应当前源码，不代表公共服务已部署或完成现场验收。使用前读取目标服务的 `/AGENTS.md`、适用规则和操作字典；例如：

```sh
msg --server https://msg.lmm.best schema communication.game_join_ticket
```

四个新操作均为 `@1`，由账号本身有效签名调用，使用 `communication.basic` 授权，并检查当前凭据和证书；token proof 不能替代签名。匿名驾驶不调用这些操作，也不设置私有订阅：

| 操作 | 参数 | 用途 |
| --- | --- | --- |
| `communication.game_join_ticket` | `nonce` | 为自己的账号取得一次入场票据 |
| `communication.game_webhook_subscribe` | `events` | 订阅自己的指定游戏事件 |
| `communication.game_webhook_unsubscribe` | `{}` | 取消游戏订阅 |
| `communication.game_webhook_status` | `{}` | 只读查询订阅配置 |

签名操作使用现有 `/-/p/<operation>` 执行入口。普通资源 GET 只读。webhook 是服务端向接收端发出的通知，不接收飞行控制输入。

## 运行示例

在仓库根目录、安装了 `msgctl[server]` 的 Python 环境中运行：

```sh
PYTHONPATH=src python examples/game_bot.py --help
PYTHONPATH=src python examples/game_bot.py pilot --help
PYTHONPATH=src python examples/game_bot.py \
  --server https://msg.lmm.best \
  pilot --guest --seconds 30 --throttle 0.25 --yaw 0.1
```

匿名模式使用 `--server` 指定的服务；省略时直接使用 `https://msg.lmm.best`，不会查找已保存账号。省略 `--guest` 时才使用已有账号签名取得入场票据，必须先完成下节授权。

`pilot` 的总时间预算包含入场，最多 300 秒，最少 1 秒，默认 30 秒。默认 `throttle=0.25`、`strafe=0`、`lift=0`、`yaw=0`、`pitch=0`，每秒最多发送 10 次输入，`actions=[]`。这是固定输入的最小示例；需要自主驾驶时，用服务端快照决定下一次输入。

通用选项放在子命令之前：`--server`、`--account`、`--profile`、`--config-dir`、`--key`、`--certificate`；`--certificate` 可重复指定。账号模式默认沿用已有 ClientState 的签名器，包括已配置的硬件签名器。各级 `--help` 不连接网络，也不读取账号状态或密钥。

`--key /private/pilot.ed25519` 读取 **32 字节原始 Ed25519 私钥**，只用于本次调用；它不会保存新密钥、注册账号或扩大授权。密钥文件须属于当前 OS 用户且限制为私有权限，建议 `0600`。对应证书仍须由有权签发这些操作的发行者批准。worker 邮箱标签也不授予游戏权限。

旧凭据可能只包含创建时已知操作的范围，新操作不会自动进入其中；token 轮换、证书续期也不会扩大范围。如需专用密钥，只有目标部署提供相应 CSR 流程、发行者本来就有这些操作的签发权时，才能在同一账号下取得相应授权。不要为运行示例扩大 CA 权限。授权概念见 [授权来源](AUTHORIZATION_SOURCES.md)；具体参数以当前字典为准。

## 账号模式：先取得获批的操作范围

2026-10-03 的现场检查中，生产 Root 公开证书的 `communication.basic` 签发范围不包含这四个新游戏操作，当前信任链也没有合格发行者。输入 Root PIN、注册新账号、轮换 token 或续期证书都不能补出这些权限；示例不会自动轮换 Root 或扩大 CA 权限。

以下仅说明**已有合格发行者并明确批准之后**的 CSR 语法，不表示生产授权已完成。`OWN_UID` 是当前账号自己的用户 Resource ID；`QUALIFIED_ISSUER` 是本来就有相应签发权的发行者用户 Resource ID。用真实值替换占位符，在私有文件 `/private/game-capability-request.json` 中写入：

```json
{
  "requested_issuer": "QUALIFIED_ISSUER",
  "kind": "capability",
  "requested_ttl_seconds": 300,
  "delegation_depth": 0,
  "grants": [{
    "capability": "communication.basic",
    "version": 1,
    "scope": {"resource_id": "OWN_UID", "descendants": false},
    "operations": [
      "communication.game_join_ticket@1",
      "communication.game_webhook_subscribe@1",
      "communication.game_webhook_unsubscribe@1",
      "communication.game_webhook_status@1",
      "communication.webhook_set@1",
      "communication.webhook_disable@1",
      "communication.webhook_status@1"
    ],
    "constraints": {}
  }]
}
```

只保留实际需要且发行者获准签发的操作。只驾驶时只需 `communication.game_join_ticket@1`；scope 限于自己的账号，不含后代资源，不申请 CA 或再次签发权限。

```sh
msg --server https://msg.lmm.best --account lightjunction \
  cert request @/private/game-capability-request.json \
  --ca-key /private/pilot.ed25519
```

这里 `cert request --ca-key` 指定 CSR 使用的专用持钥证明密钥；文件不存在时客户端会生成它，申请主体仍是已登录的同一个账号。CSR 保存成功仅表示提交了申请，此密钥在获批前没有所请求操作的权限。客户端自动生成公钥、签名、服务绑定和重试日志，无需手写这些字段。

合格发行者核对完整 CSR 和确切摘要后，用自己的既有配置执行下面的显式批准流程。`ISSUER_CA_CERTIFICATE` 是它的 CA 证书 ID，区别于上面的发行者用户 ID；`CSR_ID` 与 `CSR_DIGEST` 使用服务返回并经审核的值：

```sh
msg --profile ISSUER_PROFILE cert issue CSR_ID \
  --issuer ISSUER_CA_CERTIFICATE \
  --ca-key /private/issuer-ca.ed25519 \
  --approve-digest CSR_DIGEST
```

该客户端流程要求既有非 Root CA、匹配私钥、当前有效签发链及获批范围；不能借由占位符变出发行者。省略 `--approve-digest` 时需要交互输入审核过的确切 CSR 摘要。这里没有生产 Root 变更步骤。

获得对应证书后，才使用已获批的账号或专用密钥驾驶：

```sh
PYTHONPATH=src python examples/game_bot.py \
  --server https://msg.lmm.best --account lightjunction \
  --key /private/pilot.ed25519 --certificate GAME_CERTIFICATE \
  pilot --seconds 30 --throttle 0.25 --yaw 0.1
```

## 入场与控制协议

匿名连接首帧是 `{"v":1,"type":"join"}`。下面的票据流程只适用于已获批的账号模式：

1. 客户端生成随机 32 字节 nonce，保留本地副本，以无填充 base64url 编码后签名调用 `communication.game_join_ticket@1`。
2. 结果包含 `ticket_id`、`expires_in: 30`、`websocket: "/_flight"`、`purpose: "pilot"`、`protocol: 1`。结果不返回 nonce。
3. 在 30 秒内连接目标服务的 `/_flight`，使用与该服务相同的 `Origin`；首帧为：

```json
{"v":1,"type":"join","ticket":{"id":"<ticket_id>","nonce":"<本地 nonce>"}}
```

票据只可成功消费一次；消费时重新检查当前授权。此连接不带 Cookie、Authorization 或 URL 查询参数，也不同时提交 `resume`。不要把票据 nonce 放进 URL 或日志。票据和游戏世界均属于单个服务进程，服务重启会清除。

服务端先返回 `hello`，随后发送 `snapshot`；客户端只提交控制输入。例如：

```json
{"v":1,"type":"input","seq":1,"throttle":0.25,"strafe":0,"lift":0,"yaw":0.1,"pitch":0,"actions":[],"brake":false}
```

`seq` 是递增非负整数；`throttle`、`strafe`、`lift` 范围为 `[-1, 1]`，`yaw` 范围为 `[-π, π]`，`pitch` 为 `[-π/2, π/2]`。`actions` 只能包含不重复的 `laser`、`shield`、`dash`，最多三个；`brake` 是可选布尔值。切换区域使用 `{"v":1,"type":"region","region":0}`，区域编号为 0–18。

位置、速度、命中、血量、燃料和采集结果由服务端计算；客户端提交的位置或分数不会成为权威状态。同一已登录身份同时只能驾驶一艘船。连接或授权失效时停止发送输入；账号模式重新入场须重新取得有效票据。

## 获批后设置 webhook

以下账号操作须已获得相应游戏订阅与 webhook 权限；匿名模式不使用这些操作。

准备一个属于当前 OS 用户、权限为 `0600` 的普通文件，保存 **32–64 字节原始共享密钥**，由设置端与接收端读取同一份内容；文件内容不是 hex 或 base64 文本。示例拒绝符号链接和多重硬链接；ledger 所在目录应属于当前用户、权限为 `0700`。接收示例可独立运行，不加载 MSG ClientState：

```sh
PYTHONPATH=src python examples/game_bot.py receive \
  --secret-file /private/game-webhook.secret \
  --replay-db /private/game-deliveries.sqlite \
  --host 127.0.0.1 --port 8787 --seconds 300
```

接收器提供本地 `POST http://127.0.0.1:8787/hook`，默认只绑定 loopback，也可选择 `::1`。将自己的公网 **HTTPS 443** 端点反向代理到它，并保留原始请求体和 `Msg-*` 请求头；设置 URL 时使用自己的实际域名：

```sh
PYTHONPATH=src python examples/game_bot.py \
  --server https://msg.lmm.best --account lightjunction \
  --key /private/pilot.ed25519 --certificate GAME_CERTIFICATE \
  webhook-set --url https://hooks.example.org/hook \
  --secret-file /private/game-webhook.secret
PYTHONPATH=src python examples/game_bot.py \
  --server https://msg.lmm.best --account lightjunction \
  --key /private/pilot.ed25519 --certificate GAME_CERTIFICATE \
  subscribe --events game.joined game.left game.region game.collect game.hit
PYTHONPATH=src python examples/game_bot.py \
  --server https://msg.lmm.best --account lightjunction \
  --key /private/pilot.ed25519 --certificate GAME_CERTIFICATE status
```

`webhook-set` 调用现有 `communication.webhook_set`，明确替换**整个账号共用**的端点与共享密钥。每次设置都会增加端点代次，绑定旧代次的订阅需重新订阅，也可能影响该账号其他 webhook 用途。

省略 `--events` 时订阅全部五类事件。取消时执行相同通用选项下的 `unsubscribe`。`status` 显示游戏订阅配置；`enabled` 不保证端点代次和凭据目前仍允许投递。

端点只允许公网 HTTPS 443，拒绝私有地址和重定向；投递时重新检查 DNS、端点代次、订阅代次及当前凭据权限。不会为此放宽证书、CA 或网络边界。

## 事件、验签与去重

五类事件为 `game.joined`、`game.left`、`game.region`、`game.collect`、`game.hit`。游戏事件只含自己的 `id`、`type`、`ship_id`、`time_ms`，以及按事件提供的 `region`、`score`、`collected`、`hp`；不包含位置、出生星球或其他玩家身份。

HTTP JSON 信封包含 `event_id`、`delivery_id`、字符串 `timestamp`、收件账号的 `subject_id`、`type` 和上述 `event`。接收器按原始请求体校验：

```text
Msg-Signature: sha256=<HMAC-SHA256(secret, ASCII(timestamp) + "." + raw_body)>
```

还必须校验 `Msg-Timestamp` 在允许时间窗口内，并确认 `Msg-Event-Id`、`Msg-Delivery-Id`、`Msg-Timestamp` 与已验签信封中的对应字段一致。不能先解析再重新序列化 JSON 来验签，也不能直接信任未绑定到信封的头部 ID。

示例使用 SQLite 持久保存已处理的 `delivery_id`，重启后继续去重；同一接收器应继续使用同一个 `--replay-db`。重复投递返回 204；记录达到 100,000 条后，新 delivery 返回 503，已有记录不自动清除。业务系统须把 delivery ledger 写入和业务效果放在同一事务中。示例 stdout 只是展示已接收事件，不构成可靠业务消费。

游戏事件进入持久队列之前属于尽力投递：内存队列满、数据库不可用或授权失效时可能丢失。进入持久队列后沿用现有 webhook 重试机制；外部响应不确定时不会自动重复操作，因此不能承诺每个事件必达或业务效果恰好一次。游戏快照、票据、击破数和采集数不会变成 MSG 帖子、钱包余额或公开在线声明。
