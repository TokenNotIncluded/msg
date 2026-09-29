# 在本地验证未提交代码

验证脚本不提交、不推送、不触发 GitHub Actions，不部署生产。
它会运行项目代码和隔离测试；数据库、Valkey 必须是可销毁的本机测试实例，
不能填写生产连接。源码记录使用未提交文件的真实内容指纹，而不把 HEAD 当作测试源码。

## 准备

使用 Python 3.15，uv 0.12.20。安装锁定的开发依赖：

```sh
uv sync --locked --extra dev --python 3.15
```

系统工具包括 PostgreSQL 客户端、initdb/pg_ctl（自行启动临时集群时）、
Git/Git-LFS、age、OpenSSL、Nginx、bubblewrap。以普通用户运行。
本机 bwrap 必须能够启动隔离子进程，不能为了通过测试更改生产安全策略。

`MSG_TEST_POSTGRES_URL_TEMPLATE` 可指向专用本机测试 PostgreSQL，
库名位置保留 `{database}`，测试将创建并清理随机数据库。
不提供该变量时，由 fixture 使用 initdb/pg_ctl 启动仅 Unix socket 可访问的临时集群。
`MSG_TEST_VALKEY_URL` 必须指向专用本机测试 Valkey，不能复用生产缓存实例。

旧版本测试需要准确的固定源码，本工作区已附 `.legacy-ledger`；
在其他 checkout 中可以仅从已有 Git 对象恢复，无需提交：

```sh
mkdir -p .legacy-ledger
git archive ac083b3666b1b5268d90115129e2cc4e0dd11521 | tar -x -C .legacy-ledger
```

也可通过 `MSG_TEST_LEGACY_SOURCE` 指定等价目录。
预检与既有迁移测试会校验 manifest 中的文件哈希。

## 运行

```sh
uv run --locked --extra dev --python 3.15 python scripts/verify_local.py --preflight-only
uv run --locked --extra dev --python 3.15 python scripts/verify_local.py --shards 8
```

日志位于 `artifacts/local-<时间>/`。目录必须全新，不覆盖旧证据。
本地八个分片默认串行，避免共享一个 Valkey 时出现跨分片干扰。
脚本复用 `ci_shards.py` 的实际 pytest node-ID 并集检查，不用测试数量代替身份校验。
之后执行 conformance、打包校验和独立客户端 wheel 安装/签名传输检查。
任何阶段失败即返回非零状态；单独预检成功也不会标记整个验证通过。

`result.json` 包含基线 commit、dirty 状态、实际文件清单/摘要、命令、日志和退出码。
验证前后源码改变会使整个结果失败。所有这些结果仍然不等于生产部署验收。

## 本次受限环境中的补充测试

当前会话只有 Python 3.13，不能运行目标版本业务模块。实际通过的是以下
工具与配置测试，不加载 PostgreSQL 全局 fixture，也不代替上面的完整命令：

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest --noconftest \
  -p pytest_asyncio.plugin \
  tests/test_release_version.py tests/test_release_artifacts.py \
  tests/test_ci_shards.py tests/test_local_verification.py \
  tests/test_local_postgres_fixtures.py
```
