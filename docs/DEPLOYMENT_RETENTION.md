# 部署归档的保留与只读盘点

Issue [#217](https://github.com/TokenNotIncluded/msg/issues/217) 的历史清理尚未执行。
`scripts/plan_deployment_retention.py` 只生成文件级 JSON 清单，没有执行删除的选项，
不打开包、备份或秘密内容；它不能证明某个包确实可回退，也不改变生产服务。

部署后至少保留当前安装包，以及 **实际完成恢复与兼容性验收** 的一份回退包及其签名。
只装过旧包、服务能启动、版本较近，都不能代替 PostgreSQL/Git/CAS 恢复、schema 与
system rules、Root/证书链和关键读写行为的验证。有新业务写入后，先保护数据、暂停写入，
按 [DEPLOYMENT.md](DEPLOYMENT.md#切流与回滚) 决定回迁方案。未验证回退时，不生成清理候选。

先用 `pacman -Q msgd` 与公开的 `/usr/share/doc/msgd/build.json` 确认当前安装来源，
定位当前包、回退包和已验收的业务保护备份。以下路径必须换成实际已核验文件：

```sh
python scripts/plan_deployment_retention.py \
  --root /var/backups/msgd \
  --package-cache /home/arch/.cache \
  --current-package /home/arch/.cache/msgd-CURRENT-RELEASE-x86_64.pkg.tar.zst \
  --verified-rollback-package /home/arch/.cache/msgd-VERIFIED-RELEASE-x86_64.pkg.tar.zst \
  --confirm-rollback-tested \
  --min-age-days 7 > /secure-review/msgd-retention-plan.json
```

`--confirm-rollback-tested` 是操作者对真实验收的明确声明；规划器不执行或验证该验收。
输出按绝对路径列出文件类型、候选原因、device/inode/size/mtime/ctime/nlink，
以及可复核的候选文件数与逻辑字节数；逻辑大小不能代替实际释放空间。
文件名严格匹配 MSG 的 Arch/DEB/RPM 包才可能成为候选，
包和独立签名作为一组保护；孤立签名、硬链接、近期文件、当前包及回退包的同名副本一律保留。
默认七天保护窗同时检查 mtime 和 ctime，避免刚复制的旧包立刻被列为候选。

目录永远不是候选。业务 `service.zip`、数据库/内容备份、Root、PIN 候选记录、配置、
秘密及未识别文件一律保护；命名为 Root/keys/trust/PIN 等的子目录不遍历。
私有包缓存只盘点第一层，不进入其他项目的缓存目录。
显式选择 Root/秘密目录或其子路径也会被拒绝，不能用 `--root` 绕过保护。
根路径任一层及实际遍历到的符号链接导致整个规划失败；跨文件系统目录不遍历，重叠目录、
过宽目录、`/etc`、`/var/lib` 与共享 pacman 缓存也被拒绝。
因此未知旧归档不会因为占空间而被自动归类；其保留/退役需要单独清单与授权。

仍需操作者完成：

- 记录当前包来源、至少一份真实可回退包和业务备份的验收证据；审查 Root/证书及数据版本兼容性。
- 核对清单中的每个候选确实是历史发布物，查清运行中、恢复或其他项目的引用；复查文件身份，清单会过期。
- 另行批准并精确执行文件级清理，保留目录、保护备份及所有未知文件；不得据此执行递归删除或共享缓存清理。
- 重做目录/空间盘点并检查安装包、服务和公网入口，记录实际释放量、保留清单与恢复检查。

源码合并、规划工具测试或生成 JSON 都不表示历史清理已完成；完成现场执行和验收后才能关闭 #217。
