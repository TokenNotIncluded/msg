# 原生软件包与安装布局

使用发行版包管理器安装，不在服务器上运行 pip、uv sync 或创建 venv。`packaging/build-native.py` 从校验后的 wheel、server-dependencies.lock 和固定的独立 Python 3.15 输入构建 Arch、DEB、RPM 软件包；依赖安装只发生在临时构建目录。生产系统目前的 Python 3.14 无法运行此项目，兼容解释器由同一个系统包管理，不替换系统 Python。

| 内容 | 安装路径 |
| --- | --- |
| 命令 | `/usr/bin/msg`、`/usr/bin/msgd` |
| 应用及锁定依赖 | `/usr/lib/msgd/site-packages/` |
| 私有 Python 3.15 运行时 | `/usr/lib/msgd/python3.15/` |
| systemd 单元 | `/usr/lib/systemd/system/msgd*.service` |
| 用户和目录定义 | `/usr/lib/sysusers.d/msgd.conf`、`/usr/lib/tmpfiles.d/msgd.conf` |
| 文档及构建摘要 | `/usr/share/doc/msgctl-server/` |
| 管理员配置及公共信任 | `/etc/msgd/` |
| 业务持久数据 | `/var/lib/msgd/` |
| 根私有材料 | `/var/lib/msgd-root/`，root:root 0700 |
| 可重建缓存 / 运行时文件 | `/var/cache/msgd/`、`/run/msgd/` |
| 保护备份 / 发布归档 | `/var/backups/msgd/`、发行版包管理器的缓存目录（Arch 为 `/var/cache/pacman/pkg/`） |

运行时代码不可写；不使用 `/opt/msgd`、生产 venv 或 `/usr/local` 中的安装入口。FHS 允许应用将其私有架构相关组件放在 `/usr/lib` 的专属子目录。命令入口以隔离模式启动私有解释器，忽略 PYTHONPATH 和用户 site-packages。标准文件层级不等于官方发行版收录；这里是独立软件包，当前构建目标为 x86_64、glibc Linux。

构建要求 `uv`、`tar`、Arch 的 `makepkg/bsdtar/zstd`、Debian 的 `dpkg-deb`、RPM 的 `rpmbuild`。在匹配目标架构及 libc 基线的独立构建环境中，以普通用户运行：

```sh
python packaging/build-native.py \
  --runtime /verified/python315-prefix \
  --wheel /verified/msgctl.whl --wheel-sha256 WHEEL_SHA256 \
  --lock /verified/server-dependencies.lock --lock-sha256 LOCK_SHA256 \
  --source-revision COMMIT_AND_PATCH_DIGEST --output /build/packages --format all
```

构建输入必须从已通过验收的产物取得，不能复制生产 venv。wheel 和依赖锁校验失败会停止；Python 精确版本和输入摘要写入 `/usr/share/doc/msgctl-server/build.json`。原生扩展须在兼容基线构建。软件包生成不代表已在每种发行版完成安装验收。

```sh
sudo pacman -U ./msgctl-server-*.pkg.tar.zst
sudo apt install ./msgctl-server_*.deb
sudo dnf install ./msgctl-server-*.rpm
```

Arch 的 systemd hooks 处理用户、目录与单元重载。Debian/RPM 安装脚本执行 sysusers、tmpfiles 和 daemon-reload。首次配置数据库、初始化 CA、整理服务权限及启动服务，按 DEPLOYMENT.md 完成。软件包不包含数据库、PIN、证书、私钥和用户身份，不自动初始化 CA 或启动服务。

升级前保存保护备份，停止 writer/worker，再用包管理器升级并启动验证；卸载前停止服务。卸载保留 `/etc/msgd`、`/var/lib/msgd` 和 `/var/lib/msgd-root`。从旧 `/opt/msgd` 包升级时，包管理器删除其拥有的旧代码，管理员归档不归软件包所有的旧发布记录后移除空 `/opt/msgd` 目录，不能删除业务或根私有数据。
