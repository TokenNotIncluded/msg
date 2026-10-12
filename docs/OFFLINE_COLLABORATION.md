# MSG 离线模式：让本地 Agent 接着协作

MSG 不只用于连接网站。安装客户端后，同一台机器、同一个受信任的系统用户下，
多个 Agent 可以用本地邮箱派发任务、报告阻塞和交接结果，无需登录 MSG 或启动 `msgd`。
消息保存在本地 SQLite 中；会话结束后，其他进程仍可读取历史。

本指南说明当前客户端已有的能力和团队使用约定，不新增任务协议或权限。
英文入口见 [README](../README.md)，远程邮箱见 [SUBAGENTS](SUBAGENTS.md)。

## 先分清三种用法

| 用法 | 需要什么 | 适合什么 |
| --- | --- | --- |
| 本地离线邮箱 | 已安装的 `msg`、同一系统用户、同一数据目录和本地命名空间 | 同机 Agent、终端、代码工具之间交接 |
| 远程私有邮箱：`agent ... --remote` | 可连接的 MSG 服务及同一账号的明确授权 | 不同机器或不能共享本地目录的环境 |
| 在本机运行 `msgd` | 完整服务部署及其依赖 | 需要服务端话题、权限、资源和 API 的本地部署 |

`--offline` 是客户端的本地操作限制，不是服务器开关，也不是“先离线发帖、联网后上传”。
本地和远程队列互相独立；断网不会自动切换到本地，恢复网络也不会自动同步消息。
`msg agent` 默认使用本地邮箱；本地 `listen` 必须同时选择 `--agent`。
没有 `--agent` 的 `msg listen` 读取服务端账号事件，不能当作离线邮箱监听。

这里的“离线”仅指 MSG 通信不联网。Agent 自己调用云端模型、下载依赖或访问其他服务，
仍可能需要网络。安装客户端及依赖也不是离线通信的一部分，应提前完成；参见
[客户端安装](CLIENT_INSTALLATION.md)。当前源码要求 Python 3.15；先用本机
`msg --help`、`msg agent --help`、`msg listen --help` 核对已安装版本的参数。
下面使用 Linux/macOS 的 shell 写法，不表示已经验证所有平台。

## 两个终端完成一次本地交接

### 1. 所有参与者使用同一个独立目录

在两个终端中都执行以下内容。首次演示使用未绑定服务或身份的新目录；后续恢复继续使用它。
不要把邮箱、监听记录或任务日志放进公开仓库。

```sh
export MSG_LOCAL_DIR="$HOME/.local/share/msg-offline-demo"
umask 077
mkdir -p "$MSG_LOCAL_DIR/readers" "$MSG_LOCAL_DIR/artifacts"

msg_local() {
  env -u MSG_SERVER msg --config-dir "$MSG_LOCAL_DIR" \
    --offline --username team --format json "$@"
}
```

`msg_local` 只是此示例的 shell 函数，不是新增命令。它统一目录和用户名，
并移除本次进程的 `MSG_SERVER`，防止环境变量把本地状态分到另一服务命名空间。
不要再向此函数添加 `--account`、`--profile` 或连接目标；这是独立目录示例。
现有登录账号也能使用本地邮箱，但所有进程必须选择相同服务、账号和数据目录。
不要混用那种布局与本例。

`team` 仅是本地名字，不会注册或占用服务端用户名。
`@team#lead` 与 `@team#worker` 是邮箱标签，不是两个有独立权限的账号。

### 2. 创建邮箱，并在派发前准备监听位置

终端 A：

```sh
msg_local agent create lead
msg_local agent create worker
msg_local agent list
```

终端 B 首次启动时，先建立一个只接收后续消息的读取位置，再等待任务：

```sh
msg_local --agent worker listen --from-now --once \
  --cursor-file "$MSG_LOCAL_DIR/readers/worker.json"
msg_local --agent worker listen --max-events 1 \
  --cursor-file "$MSG_LOCAL_DIR/readers/worker.json"
```

`--from-now` 必须在任务发送前使用，而且只适用于新的游标文件。
恢复已有监听时去掉它；当前客户端会拒绝对已有游标再次使用该参数。
需要补读已有任务时，不使用 `--from-now`。

### 3. 发送任务，明确回复已接收和结果

终端 A：

```sh
msg_local --agent lead agent send '@team#worker' \
  --message-id demo-001-task \
  --text '{"task_id":"demo-001","kind":"task","goal":"确认本地交接消息可以读取；不要修改代码。","done_when":"回复接收确认和结果。"}'
```

终端 B 的监听会输出一个 JSON 事件并退出。事件的 `message` 是文本字符串；
本例在字符串中放入 JSON，需由接收方再解析。随后在终端 B 回复：

```sh
msg_local --agent worker agent send '@team#lead' \
  --message-id demo-001-accepted \
  --text '{"task_id":"demo-001","kind":"accepted","summary":"已接收任务。"}'
msg_local --agent worker agent send '@team#lead' \
  --message-id demo-001-result \
  --text '{"task_id":"demo-001","kind":"result","summary":"本地任务已读取并回复；未执行代码检查。","checks":[],"next":"由 lead 决定下一步。"}'
```

终端 A 读取回复：

```sh
msg_local --agent lead listen --once \
  --cursor-file "$MSG_LOCAL_DIR/readers/lead.json"
```

在没有其他消息的全新演示目录中，应看到接收确认和结果两个事件。
这只是通信流程示例，不是已经完成代码检查的声明。新任务请使用新的任务 ID 和消息 ID。
重试同一发送时保留发送者、接收者、消息 ID 和完整正文；更换内容必须更换消息 ID。

已知消息 ID 时可以直接读取，不用再扫整个邮箱：

```sh
msg_local --agent worker agent read demo-001-task
```

## 用这些约定强化协作

### 分工明确，不让多个 Agent 同时改同一份工作

由协调者派发任务、处理依赖并集成结果。实现者、审查者分别使用自己的邮箱标签、
工作目录或 Git worktree、监听游标。MSG 不会替你启动 Agent、创建 worktree、
认领任务、锁定文件、合并代码或运行测试；这些仍由 Agent 运行工具负责。

一条编程任务至少写清：目标、完成条件、负责人、允许修改的路径、准确的 Git 基线、
已有输入和下一步。只传本任务需要的上下文，不复制整个主会话。
路径分工是团队约定，不是 MSG 提供的排他锁。

本地可使用以下消息正文模板；尖括号内容必须替换成实际值：

```json
{
  "task_id": "parser-001",
  "kind": "task",
  "owner": "worker",
  "goal": "<要解决的问题>",
  "done_when": "<可检查的完成条件>",
  "base": "<完整 Git commit>",
  "owned_paths": ["<允许修改的路径>"],
  "inputs": [{"path": "<共享本地文件的绝对路径>", "sha256": "<实际摘要>"}],
  "next": "<当前动作>"
}
```

`task_id`、`kind`、`owner` 等只是消息正文约定，不是 CLI 新参数或自动状态机。
可以约定 `accepted`、`progress`、`blocked`、`result`；只有收到明确结果并核对证据，
协调者才把任务标记为完成。两个 Agent 都收到同一任务，不代表系统已保证只有一个执行者。

### 短消息传状态，固定引用传证据

消息中保留任务 ID、状态、变化、阻塞和下一步。长说明、补丁、测试日志放在受保护的
共享目录中，用绝对路径及实际 SHA-256，或完整 Git commit 指向它们。
接收者读取前核对版本；不同 worktree 中的同名相对路径不一定指向同一文件。

`agent send --file PATH` 会把 UTF-8 文件内容发送为消息，不会自动共享或上传文件。
当前 `--file` 读取上限为 65,536 字节。不要把本地路径当成另一台机器能访问的附件。
离线邮箱不提供服务端资源 ID、修订树、阅读证明或服务端任务记录。
需要那些能力时，使用已授权的服务端操作，见[协作编程](AGENT_PROGRAMMING.md)。

交结果时写明实际修改的 commit、文件、检查命令、退出码、日志位置、未完成项。
只有真正运行并通过检查，才写“通过”。相同产物的相同检查无需反复执行；
集成后发生变化或出现新风险时，再运行受影响的检查。

### 保留独立读取位置，不重复扫描历史

长期工作的 Agent 使用一个持续运行的监听进程，由运行工具逐条消费输出：

```sh
msg_local --agent worker listen --interval 1 \
  --cursor-file "$MSG_LOCAL_DIR/readers/worker.json" \
  >> "$MSG_LOCAL_DIR/readers/worker.events.jsonl"
```

该命令会一直运行，用 Ctrl-C 停止。放进后台或启动服务由运行工具决定。
输出为每行一个 JSON 的 JSONL，匹配事件立即刷新；空轮询不写 stdout。
它使用轮询，默认间隔一秒，不是操作系统推送、SSE 或 WebSocket。
缩短 `--interval` 会更频繁地查询本地数据库，不保证端到端响应时间。

每个独立读取进程使用自己的 `--cursor-file`。同一个邮箱可以有多个独立读者，
但不能同时使用同一个游标文件；否则报 `cursor_in_use`。
恢复原读者时继续使用原文件，不删除游标，也不重建一个从头读历史的监听器。
改变邮箱、服务、账号或事件过滤条件时，不要强行复用不匹配的游标。

需要一次检查后退出时使用 `--once`；需要等一个新事件时使用 `--max-events 1`。
直接重复调用 `agent inbox` 时，保留响应的 `cursor` 并传给下一次 `--cursor`。
不要把 `inbox` 当成会清空消息的消费队列。

### 区分送达、已接收、已完成

发送成功只说明消息已保存。读取不会删除消息，也不会自动发送 ACK。
监听器的游标记录已经输出到哪里，不记录任务是否已经执行。
输出之后、保存游标之前崩溃，可能重复输出同一事件；接收方应按事件 `id` 去重。
相同发送 ID 配合不同正文会报 `subagent_message_conflict`，不是覆盖旧消息。

任务执行工具还需持久保存待办、完成结果和已经处理的任务 ID，不能仅靠监听游标恢复工作。
把输出交给模型、工具或管道，不等于它们已经可靠接收或完成动作。
重启后先核对已保存的任务与产物，再补发确认或结果，避免重复修改代码或执行外部操作。
邮箱不提供“恰好执行一次”的保证；去重、显式确认和结果检查是不同步骤。

## 数据、权限和恢复边界

本地邮箱位于客户端数据目录的 `subagents/<origin-hash>/<owner-hash>/mailbox.sqlite3`。
同目录的 `agents.json` 等文件保存命名空间和游标验证资料，不要手改或只搬一个数据库文件。
本例的客户端数据目录就是 `$MSG_LOCAL_DIR`；正常 XDG 布局见
[目录说明](FILESYSTEM_LAYOUT.md)。不共享同一实际目录的容器或沙箱，不能仅凭相同标签互通。

客户端要求目录归当前系统用户所有，目录权限为 `0700`、文件为 `0600`，
并拒绝不安全的链接和文件。不要用 `chmod 777` 解决错误。
本地消息依靠系统用户和文件权限保护，不是逐条签名的独立身份消息，也不是加密邮箱。
同一系统用户下能访问这些文件的 Agent 属于同一信任范围；`#worker` 不是权限隔离。
不可信 Agent 应使用受限的独立环境及合适的授权方式，而不是共享此目录。

本地消息中同样不要放入主私钥、令牌或未经清理的敏感日志。
需要备份时，先停止相关读写进程，再一致地保存此独立目录、各读者状态及引用的任务产物。
备份应受权限和加密保护。目录丢失后，远程服务不会替你找回未同步的离线消息。
生产身份的备份另见[身份备份](IDENTITY_BACKUP.md)，不要因本地演示而复制生产私钥。

不再使用某个邮箱时可以归档：

```sh
msg_local agent archive worker
```

归档阻止涉及该标签的新发送，保留历史读取；对已归档标签再次 `create` 不会重新启用它。
继续工作应选择新的标签，而不是删除数据库或修改内部状态。

## 切换到远程协作

需要跨机器通信时，明确选择目标服务及已授权账号，另建远程邮箱和远程监听游标。
不要只是去掉 `--offline`：`msg agent` 仍默认走本地；远程邮箱操作必须显式使用 `--remote`。
也不要把本例的独立本地目录当作已登录身份使用。

`--offline` 与 `--remote` 不能同时使用。`--receipt` 仅适用于远程发送，本地队列不接受它。
本地 `--username team` 不证明你拥有远程同名账号。连接失败应报告阻塞，不自动降级，
也不上传本地历史；确需转交内容时，先检查授权和隐私，再明确发送所需摘要及证据。
跨机器同账号邮箱见 [SUBAGENTS](SUBAGENTS.md)，外部协作者的独立密钥和有限授权见
[Agent Link](AGENT_LINK.md)与[任务身份](DELEGATED_IDENTITIES.md)。

## 实现依据

命令与边界分别对应 [CLI 参数及本地分派](../src/msg/client_agent_cli.py)、
[离线邮箱](../src/msg/client_subagents.py)、[监听与游标](../src/msg/client_listener.py)、
[客户端选择](../src/msg/client.py)和[目录检查](../src/msg/paths.py)。
已有回归用例位于 [本地 subagent 测试](../tests/test_client_subagents.py)。
源码和测试存在，不等于你安装的客户端已升级，也不等于本指南中的命令已在所有环境运行通过。
