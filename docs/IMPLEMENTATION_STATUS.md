# 实现范围与验收边界

本次代码基于设计文档快照，不依赖旧版实现。`docs/provenance.json` 记录快照摘要与重写起点。此目录不是旧项目的兼容层，也不包含旧数据库自动迁移器。

| 设计范围 | 代码位置 | 状态 |
| --- | --- | --- |
| 统一模型、有限类型与九个 Protocol | core/models.py、contracts.py、registry.py | 已实现；运行时不使用空占位业务 |
| 事务、幂等、当前授权、回执、审计 | core/executor.py、storage/sqlite.py | 实际测试覆盖 |
| 内部话题 Git 与二进制内容 | storage/git.py | 实际临时 bare Git / 内容持久化测试 |
| 身份、组织、临时升级、凭据上限、证书链 | security/、plugins/identity.py | 实际签名 / CA 测试与 18 项特殊能力矩阵 |
| 帖子、回复、模板、文件、归档、发现 | plugins/ | 实际业务、权限、分页与生命周期测试 |
| HTTP / 路径 GET / CLI / MCP | transports/、client.py、cli.py | 五条入口完整分片流程实际通过 |
| GraphQL | transports/graphql.py | 实现与强制验收已写；本地缺 graphql-core 未执行 |
| 根初始化、签发、撤销、PIN、轮换 / 恢复 | admin/root.py、rotation.py | 内部真实密码与数据流程测试；物理控制台须部署验收 |
| 受限 SSH 与 Git 引用提交前复核 | extensions/ssh*.py | 命令解析、认证、实际 Git hook 测试；未做真实 sshd 握手 |
| DNS / HTTP 受控 worker | security/network.py、workers/sandbox*.py | 策略与状态测试；本地无 bubblewrap，完整隔离进程须验收 |
| 密钥库、静态发布、原生公开 Git | extensions/ | 本地流程与权限测试 |
| 邮件验证、消息通知、SMTP 重试边界 | identity.py、communication.py、workers/mail.py | 代码与本地状态覆盖；无真实 SMTP/TLS 投递验收 |
| RSS 与 SubHub | extensions/rss.py | RSS 2.0 与统一读取权限已实现；设计未给出 SubHub 特有协议，未声称兼容未定义接口 |
| doctor / selftest、备份恢复、清理 | admin/、workers/maintenance.py | 实际隔离安装 / 恢复测试 |
| Python 3.15 与 CI | pyproject.toml、.github/workflows/ci.yml | 目标及强制验收门槛；当前容器只运行过 3.13.5 |

## 不能混为一谈的事项

实现存在不等于已在真实部署验证。没有向远程仓库推送，所以本次没有 GitHub Actions 运行结果；没有更新线上实例、自动合并或发布 release。

消息邮件通知当前覆盖邮箱验证及 communication.send。回复、提及、证书状态等全部事件的邮件订阅投影并未做完整端到端实现与验收，不应当作已经支持的通知选项。统计目前以业务事实、ACK 清单和计数为主，不承诺完整浏览量 / 下载量采集。

初始化发生部分落盘故障时会明确要求恢复，不静默替换根。现有 recover 用于可信根材料的加密备份恢复，rotation --resume 用于轮换日志恢复；任意初始化阶段损坏的自动补全不是本交付已经验证的能力。

审计只提供相对可信检查点的可检测性，不宣称有磁盘写权限的人不能重算历史。purge 不清除已导出的备份或第三方副本。无账号配额不代表物理磁盘、单次报文和执行时间无穷。

上述范围说明保留了设计与实现之间的差异，不将这些差异藏进“全部完成”的表述。
