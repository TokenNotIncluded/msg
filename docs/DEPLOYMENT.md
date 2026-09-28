# 部署

## 前置条件

服务器目标是 Linux、Python 3.15、Git、PostgreSQL。Valkey 用于可选的短期信号与缓存；服务必须在不连接 Valkey 时仍能从 PostgreSQL 恢复待处理工作。SSH 扩展需要 OpenSSH，网络工具 worker 需要 bubblewrap。普通客户端不需要运行系统服务，但必须能保存自己的私钥；不能安装软件的 Agent 使用 HTTP / 纯路径协议。

先在新数据目录验收，不要将新服务直接指向旧版数据库。没有自动迁移器。生产前阅读 SECURITY.md 和 VERIFICATION.md。

先创建专用 PostgreSQL 数据库与最小权限账号，再配置 `/etc/msgd/msgd.toml`：

```toml
[storage]
postgres_dsn = "service=msgd"
# 可选：只用于唤醒与缓存，不保存唯一业务状态
valkey_url = "redis://127.0.0.1:6379/0"
content = "/var/lib/msgd/git/content"
repositories = "/var/lib/msgd/git/repos"
blobs = "/var/lib/msgd/blobs/sha256"
staging = "/var/lib/msgd/transfers/staging"
service_keys = "/var/lib/msgd/service"
```

`postgres_dsn` 必填；示例使用 libpq 的 service 名称，连接主机、数据库名和凭据应放在仅服务账号可读的 service 文件或 libpq 环境配置中。`msgd.toml` 由 root 持有、msgd 组可读（0640），仍应优先避免在其中放密码。如使用 URL，也支持 `postgresql://` 或 `postgres://`。`valkey_url` 可省略；Valkey 客户端接受 `redis://`、`rediss://` 与 `unix://`。旧安装只有 `server.toml` 时仍可读取；两种文件同时存在会拒绝启动，迁移时须明确选择一份。

## 安装代码

```bash
sudo install -d -m 0755 /opt/msgd
sudo python3.15 -m venv /opt/msgd/venv
sudo /opt/msgd/venv/bin/python -m pip install '.[server]'
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

持久数据位于 `/var/lib/msgd/`，可重建缓存位于 `/var/cache/msgd/`，运行时文件位于 `/run/msgd/`；这些目录不能互换，未 seal 的分片必须保留在持久暂存中。配置目录 `/etc/msgd/` 只放服务配置与公开信任材料。根 CA 私有状态位于 `/var/lib/msgd-root/`，root:root 0700，根加密私钥 0600；`/var/lib/msgd/service/` 为 root:msgd 0750，在线与回执私钥 root:msgd 0640。trust 公共材料只允许管理员修改。**不要递归 chown `/etc/msgd/` 或 `/var/lib/msgd-root/` 给服务账号。** 旧安装的根材料若仍在 `/etc/msgd/root/`，需明确迁移与复核，不能靠目录名推断已经完成。

systemd 配置不预设禁止 worker 使用 user namespace 的系统调用；bubblewrap 需要宿主允许其隔离机制。执行失败应排查宿主配置，不能删除 worker 的隔离要求作为修复。

HTTP 默认监听 127.0.0.1:8042。反向代理 Host 必须与 service_url 匹配。无跨来源 CORS放行，不共享管理员 Cookie。新 Nginx 配置位于 deploy/nginx.conf，TLS 证书路径需由部署者填写。

## 可选扩展

当前托管由主app同域匿名只读服务，所有托管响应强制CSP sandbox且当前禁JS，危险格式按附件下载。@root/web已有真实Resource；private preview需签名header，不能把URL当无凭据浏览器导航。已有局部发布/回滚和浏览器证据不等于完整同域安全矩阵，生产开放前仍须验证当前部署。

邮件默认关闭。复制 mail.example.toml 到 `/etc/msgd/mail.toml` 后填写真实 TLS SMTP参数。认证凭据使用独立 0640 文件引用，不放入根目录、不提交仓库。worker发送失败不回滚业务；未知 DATA 结果由管理员或上层投递策略处理。

SSH 使用专用端口和单独 sshd 配置 deploy/sshd_config，不能覆盖现有管理员 SSH。其服务账号必须只允许公开密钥+受限命令，不允许密码、PTY、转发、用户启动脚本或 SFTP。AuthorizedKeysCommand动态验证本站已登记 SSH 公钥。注册需要对应私钥的持钥证明与明确 ceiling，不靠复制一行 authorized_keys 绕过账号授权。

## 备份、恢复与根轮换

```bash
# 备份归档中包含 PostgreSQL dump 与内容文件。
msgd backup /secure-backup/service.zip
# 在隔离的新实例预先创建空 PostgreSQL 数据库，并让 libpq 的 service=msgd 指向它。
msgd --config-dir /new/etc/msgd restore /secure-backup/service.zip --data-dir /new/var/lib/msgd
```

`msgd backup` 的 v4 归档包含 PostgreSQL dump、内部文本 Git、公开仓库、Blob/CAS、可恢复暂存、公共信任及服务密钥，**不含根私钥**；恢复只接受v4，旧SQLite、v2/v3归档不能直接恢复。恢复命令默认使用 libpq 的 `service=msgd`，该 service 必须指向新建的空目标数据库；目标配置目录与数据目录也必须不存在。v4核验PostgreSQL/Git/CAS/LFS引用。隔离restore默认写暂停，并设置worker/daemon禁外发marker；必须显式人工核验并提升后才能运行或切流，不能恢复完自动作为生产启动。生产在线备份尚未演练，外部Git写可能使一致性检查fail-closed。Valkey 的短期数据无需备份。备份是敏感文件，保存为 0600 并在外部加密。根材料单独从本机控制台执行 `msgd root backup PATH`，恢复时核验现有信任锚。

改 PIN 使用 `msgd root change-pin`，不改变公钥。轮换使用 `msgd root rotate`，根遗失则显式 `--lost-key`，中断恢复用 `--resume`。轮换前停止服务和 worker，完成后重新签发基础在线 CA、复核权限、重启，通知客户端更新信任 / 重新申请授权。普通账号可以 `msg cert renew` 获取新的基础证书；特殊授权与下级 CA 仍需重新审核。

## 上线验收

执行 tests 与 conformance，确认 Python 3.15、GraphQL、MCP、CLI分片一致性；用真实 sshd 完成登录、拒绝 shell、拒绝转发、Git push撤销测试；用真实 bubblewrap 验证 worker 看不到 `/var/lib/msgd-root/`、服务数据与凭据；验证公网目标、私网拒绝和重定向检查；用隔离 SMTP 测试 TLS、禁用状态、连接重试和 uncertain。

只有所有适用项在真实部署通过，才能判断是否允许生产流量。本地 Python 3.15 测试不能代替部署环境的运行入口检查。
