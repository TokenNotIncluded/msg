# 传输与线协议

## 契约发现

`GET /_transports` 返回部署的 operation effect、版本摘要、路径 / 请求 / 响应上限与推荐分片大小。`GET /!operation` 只描述，不执行；`/schema` 返回输入输出契约。业务资源默认 Markdown；JSON、元数据、原件、历史与固定修订使用统一后缀。

完整操作 schema 见 [operations.json](operations.json)。示例中的 RESOURCE_ID、REVISION_ID、REQUEST_ID 是占位符，调用时替换为实际结果。

## 一个签名包

OperationRequest 包含协议版本、请求 ID、operation / contract_version、target_service、subject、arguments、expected_generations、expires_at、payload_digest、proof、return_fields 与 source。

JSON 拒绝重复键、非有限浮点数和未声明字段。规范编码由 core/codec.py 给出：UTF-8、键排序、紧凑分隔符；**不是 RFC 8785 / JCS 兼容声明**。算法和版本由本站契约固定，其他语言应使用发布测试向量验证，不能假定其默认 JSON 浮点格式相同。

payload_digest 覆盖业务内容，排除 proof、source、expires_at；请求签名覆盖除 proof 外的全部确定性编码。因而可以为同一请求 ID 与内容重新签署新的短有效期，而不能改变操作参数。签名用途通过 Ed25519 的版本化前缀分离。内容签名绑定不可变 Revision 清单；服务器回执不替代原作者内容签名。

同主体同 request_id、同摘要只提交一次；同键不同内容返回 idempotency_conflict。重试仍先检查当前身份与内容可见性，不把幂等缓存当授权缓存。

## HTTP 与纯路径 GET

```text
POST /!content.post_create           写操作
POST /~discovery.get                 带签名的查询
GET  /~discovery.get/run/j/PACKET    纯路径查询
GET  /!content.post_create/run/j/PACKET
GET  /!content.post_create/schema   schema
HEAD 任意普通读取或 run 地址          不执行写入
```

PACKET 是 UTF-8 JSON 的 Base64URL；gz 表示 gzip 后的 Base64URL。纯路径写 URL 含凭据材料，不要发送给预览机器人、记录到访问日志或复用为普通导航链接。默认关闭服务访问日志，反向代理也必须避免完整记录它。

`/raw` 对二进制返回真实字节并支持单段 HTTP Range；复杂或受限环境统一使用 transfer。ETag 是条件读取机制，不代表授权可以缓存。

## GraphQL

入口 `POST /-/graphql`。query 只读，mutation 只调用已声明写操作；它们映射到同一个执行器。通用入口：

```graphql
mutation MsgOperation($packet: JSON!) {
  call(packet: $packet)
}
```

还会由注册表生成 `content_post_create` 等字段。远程传来的包仍需相同签名 / 令牌。实现依赖真实 graphql-core，不用字符串匹配冒充 GraphQL。

## MCP

本地：`msg mcp`；远程：`/mcp`。协议固定支持的版本由 transports/mcp.py 公布。Streamable HTTP 使用 POST JSON 响应模式；没有 SSE 监听时 GET 返回 405。通知接受后返回 202 空响应。stdio 标准输出不混入教程或日志。

本地工具接收业务参数，msg 负责签名。`params._meta["msg/request_id"]` 可指定重试 ID，`msg/expected_generations` 指定版本。远程工具接收 `arguments.packet`，由调用者提供完整证明。两者都不暴露根管理工具。

## 双向 transfer

六个原语在所有入口复用：open、part_put、part_get、status、seal、cancel。上传 open 声明大小、摘要和更严格的客户端上限；part_put 包含 transfer_id / offset / data / digest；seal 使用 **final_size / final_digest**。

分片可以乱序、可变长但不能不一致重叠。同范围同内容幂等，不同内容冲突；seal 检查无缺口和整体摘要。下载 open 固定资源修订，每次分片仍按当前权限读取。status 的缺失区间可分页；连接断开不丢失主体绑定的会话。

## 小响应与游标

写结果不默认返回整篇正文，使用 return_fields 显式请求。列表按需选择 fields，下一页由完整 next 地址给出。私有下一页仍需重新附加身份；next 链接不是访问令牌。同步游标与分页游标分开，授权变化或同步窗口不可用时返回 resync_required。

传输丢失响应不是事务失败证据。使用同一个 request_id 重试，不能因为没有收到回复就生成另一个写请求。外部任务 returned accepted 只表示已可靠入队；最终状态读取 job.get。
