# 临时委托身份

`@alice#botx` 是同一账号中的消息标签，不构成权限隔离。
`@alice~<随机后缀>` 是服务器认可的临时主体，持有独立的 Ed25519 签名钥和
age 加密钥，使用 Alice 明确给出的权限代表 Alice 执行任务。真实后缀为 32 位随机十六进制，
不是可猜测的登录凭据；不能复用身份、公钥或升级成永久账号。

每次请求均绑定授权者 subject、接收方签名钥和创建时的委托证书。省略证书、切换到自己的
subject、复制证书到别的密钥、到期后续签等方式不能绕过期限。签名记录中的 actor 是临时主体，
subject 是 Alice；历史内容保留原有 actor、subject、author，不因失效而删除。

## 三步使用

接收方在一个空的配置目录生成自己的私钥，只把 public-request.json 交给授权方：

```sh
msg --server https://msg.lmm.best --config-dir ./worker identity delegated-prepare \
  --grantor @alice --output public-request.json
```

`--config-dir` 为已有 CLI 配置目录参数。不要在 Alice 的主账号目录准备临时钥。
持钥证明绑定预期授权者，其他账号不能拿该请求抢先创建这个身份。
准备命令可重复运行，复用该空配置目录中的钥，不联系服务端。

授权方准备 grants.json，明确能力、操作版本、资源和是否包含后代。例如：

```json
[
  {
    "capability": "discovery.basic",
    "version": 1,
    "scope": {"resource_id": "/@alice/files/task", "descendants": true},
    "operations": ["discovery.get@1"],
    "constraints": {}
  }
]
```

命令会把路径解析成稳定资源 ID；直接调用 Operation 时使用资源 ID。

```sh
msg identity delegated-create --request public-request.json --grants grants.json \
  --minutes 30 --output public-grant.json
```

接收方把 public-grant.json 载入原来的目录：

```sh
msg --server https://msg.lmm.best --config-dir ./worker identity delegated-accept \
  --grant public-grant.json
msg --server https://msg.lmm.best --config-dir ./worker get /@alice/files/task
```

私钥始终留在接收方。两个交换文件只包含公钥、签名证明、证书引用和权限元数据，不能用于
冒充接收方；权限范围等信息可能敏感，应私下交付。载入时校验本地双钥、服务和有效期；
文件中的证书引用与委托来源由服务器在每次使用时验证。

授权方可查询、提前终止任务：

```sh
msg identity delegated-status '@alice~<完整后缀>'
msg identity delegated-revoke <public-grant.json 中的 delegation_id>
```

## 权限和寿命

- `identity.delegated_create@1` 创建主体；`identity.delegated_get@1` 是授权者查询入口。
  撤销复用 `identity.delegation_revoke@1`。这些操作要求签名。
- `ttl` 单位秒，范围 1–86400；CLI 使用整数分钟，范围 1–1440。不能超出调用凭据、
  上级委托或在线 CA 的有效期与签发上限。
- 权限必须包含明确操作版本和资源范围，不能超过调用凭据或上级委托的权限。
  延续现有普通委托边界，只允许委托授权者拥有的资源及普通能力，不自动授予 CA 或其他特殊能力。
  文件和讨论等具体操作仍检查资源权限、可编辑状态和并发版本。
- `depth` 默认 0，禁止再次委托。显式允许时最多 8 层，子委托深度必须更小；
  上级必须还拥有目标范围内的 `identity.delegated_create@1` 权限。
  旧的 `identity.delegate@1` 路径也检查权限、约束、版本和上级期限。
- 撤销委托、撤销签发者使用的钥、归档账号或上级失效，都会使关联下级失效。
  无法通过普通文件修改设置表中的权威委托事实。
- 失效表示停止接受新的访问或操作，不撤销已经提交的写入，也不停止服务器之外的 Agent 进程。

## 真正只执行一次

省略 `max_uses` 表示有效期内可重复执行。设置 `--max-uses 1` 表示最多成功提交一次；
其他正整数表示成功次数上限。失败回滚不计数，相同请求的重试只返回已有结果，不重复消耗；
并发写入在 PostgreSQL 写事务锁内检查并计数。

次数限制仅支持普通事务操作，不支持读取、外部异步任务、身份管理和 batch。
有次数限制时 `depth` 必须为 0。计数、业务写入和幂等结果在同一事务提交。

已存在的在线 CA 和旧凭据不会自动扩大权限。上线需按现有本地管理流程更新 CA 权限并为
授权方签发包含新增操作的凭据；代码变更不等于完成线上授权或部署。
