# 部署

## 前置条件

服务器目标是 Linux、Python 3.15、Git、PostgreSQL。Valkey 用于可选的短期信号与缓存；服务必须在不连接 Valkey 时仍能从 PostgreSQL 恢复待处理工作。SSH 扩展需要 OpenSSH，网络工具 worker 需要 bubblewrap。普通客户端不需要运行系统服务，但必须能保存自己的私钥；不能安装软件的 Agent 使用 HTTP / 纯路径协议。

先在新数据目录验收，不要将新服务直接指向旧版数据库。没有自动迁移器。生产前阅读 SECURITY.md 和 VERIFICATION.md。

先创建专用 PostgreSQL 数据库与最小权限账号，再配置 `/etc/msgd/server.toml`：

```toml
[storage]
postgres_dsn = "service=msgd"
# 可选：只用于唤醒与缓存，不保存唯一业务状态
valkey_url = "redis://127.0.0.1:6379/0"
content = "/var/lib/msgd/content"
staging = "/var/lib/msgd/staging"
```

`postgres_dsn` 必填；示例使用 libpq 的 service 名称，连接主机、数据库名和凭据应放在仅服务账号可读的 service 文件或 libpq 环境配置中。`server.toml` 由 root 持有、msgd 组可读（0640），仍应优先避免在其中放密码。如使用 URL，也支持 `postgresql://` 或 `postgres://`。`valkey_url` 可省略；Valkey 客户端接受 `redis://`、`rediss://` 与 `unix://`。不要将 PostgreSQL 或 Valkey 直接暴露到公网。

## 安装代码

```bash
sudo install -d -m 0755 /opt/msgd
sudo python3.15 -m venv /opt/msgd/venv
sudo /opt/msgd/venv/bin/python -m pip install .
```

在真实本机 VT 或串行控制台运行：

```bash
sudo /opt/msgd/venv/bin/msgd init --service-url https://msg.example.org
sudo /opt/msgd/venv/bin/msgd cert issue ONLINE_CA_REQUEST_ID
```

ONLINE_CA_REQUEST_ID 取自 init 输出。签发时检查显示的权限范围和申请摘要，输入准确摘要确认，再输入 PIN。没有默认 PIN、环境变量 PIN 或 --yes。不能通过 SSH / 远程命令转发完成 root 管理；不能把退出码 78 当成初始化成功。

根初始化会保留 @root、公钥、根证书，以及待授权基础在线 CA。普通注册在 CA 尚未授权时返回 issuer_not_ready，不借用根私钥。

## 文件权限与系统服务

管理员审核后执行 `deploy/prepare-service.sh`，它只准备运行账号和文件权限，不生成根、不签证书、不覆盖配置。将新 systemd 单元复制到 `/etc/systemd/system/`，检查其中的固定安装路径，再运行 daemon-reload 和 enable。

```bash
sudo sh deploy/prepare-service.sh
sudo install -m 0644 deploy/msgd.service deploy/msgd-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now msgd.service msgd-worker.service
sudo /opt/msgd/venv/bin/msgd doctor
```

数据目录由 msgd 持有；配置目录由 root 持有。`root/` root:root 0700，根加密私钥 0600；`service/` root:msgd 0750，在线与回执私钥 root:msgd 0640。trust 公共材料只允许管理员修改。**不要递归 chown 整个 /etc/msgd 给服务账号。**

systemd 配置不预设禁止 worker 使用 user namespace 的系统调用；bubblewrap 需要宿主允许其隔离机制。执行失败应排查宿主配置，不能删除 worker 的隔离要求作为修复。

HTTP 默认监听 127.0.0.1:8042。反向代理 Host 必须与 service_url 匹配。无跨来源 CORS放行，不共享管理员 Cookie。新 Nginx 配置位于 deploy/nginx.conf，TLS 证书路径需由部署者填写。

## 可选扩展

静态托管在 server.toml 中设置不同主机名的 public_web_origin，使用 `msgd hosting` 监听独立入口。身份站点与用户网页不能同源。没有启用该来源时不得把 hosting 路由混入 API 站点。

邮件默认关闭。复制 mail.example.toml 到 `/etc/msgd/mail.toml` 后填写真实 TLS SMTP参数。认证凭据使用独立 0640 文件引用，不放入根目录、不提交仓库。worker发送失败不回滚业务；未知 DATA 结果由管理员或上层投递策略处理。

SSH 使用专用端口和单独 sshd 配置 deploy/sshd_config，不能覆盖现有管理员 SSH。其服务账号必须只允许公开密钥+受限命令，不允许密码、PTY、转发、用户启动脚本或 SFTP。AuthorizedKeysCommand动态验证本站已登记 SSH 公钥。注册需要对应私钥的持钥证明与明确 ceiling，不靠复制一行 authorized_keys 绕过账号授权。

## 备份、恢复与根轮换

```bash
# 备份归档中包含 PostgreSQL dump 与内容文件。
msgd backup /secure-backup/service.zip
# 在隔离的新实例预先创建空 PostgreSQL 数据库，并让 libpq 的 service=msgd 指向它。
msgd --config-dir /new/etc/msgd restore /secure-backup/service.zip --data-dir /new/var/lib/msgd
```

`msgd backup` 生成的归档需包含 PostgreSQL dump、与其一致的内容文件、公开仓库、公共信任及服务密钥，**不含根私钥**；不能把旧 SQLite 文件当作可恢复的数据库。恢复命令默认使用 libpq 的 `service=msgd`，该 service 必须指向新建的空目标数据库；目标配置目录与数据目录也必须不存在。先在隔离环境核验归档和恢复结果，再切换服务流量。Valkey 的短期数据无需备份。备份是敏感文件，保存为 0600 并在外部加密。根材料单独从本机控制台执行 `msgd root backup PATH`，恢复时核验现有信任锚。

改 PIN 使用 `msgd root change-pin`，不改变公钥。轮换使用 `msgd root rotate`，根遗失则显式 `--lost-key`，中断恢复用 `--resume`。轮换前停止服务和 worker，完成后重新签发基础在线 CA、复核权限、重启，通知客户端更新信任 / 重新申请授权。普通账号可以 `msg cert renew` 获取新的基础证书；特殊授权与下级 CA 仍需重新审核。

## 上线验收

执行 tests 与 conformance，确认 Python 3.15、GraphQL、MCP、CLI分片一致性；用真实 sshd 完成登录、拒绝 shell、拒绝转发、Git push撤销测试；用真实bubblewrap验证worker看不到 `/etc/msgd/root`、服务数据与凭据；验证公网目标、私网拒绝和重定向检查；用隔离 SMTP 测试 TLS、禁用状态、连接重试和 uncertain。

只有所有适用项在真实部署通过，才能判断是否允许生产流量。本地 Python 3.15 测试不能代替部署环境的运行入口检查。
