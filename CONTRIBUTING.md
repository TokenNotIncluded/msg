# 贡献约定

设计契约优先于旧实现。不要把历史 `src/msgd/`、旧计费逻辑、标签逻辑、旧网页或旧测试复制回来。

变更流程是：先加入能失败的行为测试，再实现最小修改，最后在测试继续通过时重构。权限、身份、CA、路径解析、幂等和分片必须包含拒绝路径。不要用跳过、空返回、放宽断言或只测试手工构造的已认证主体取得绿色结果。

业务 handler 返回 HandlerOutput，不自行 commit、不伪造 actor、不自己宣布执行成功。协议适配器不能另建授权规则或分片状态机。新增资源、能力、操作、schema 与关系必须显式登记。系统凭据与组成员不是可任意编辑的普通文件。

## 本地检查

```bash
python3.15 -m pip install -e '.[dev]'
python3.15 -m compileall -q src
python3.15 -m pytest tests
python3.15 -m pytest conformance
python3.15 -m build
```

`tests/` 包含需要 PostgreSQL 的集成测试，以及真实 Git、SQLite、Ed25519 和套接字测试。未设置 `MSG_TEST_POSTGRES_URL_TEMPLATE` 时，根目录的 `conftest.py` 会尝试通过本机 `initdb` 和 `pg_ctl` 启动临时数据库。`conformance/` 包含强制运行时、完整传输与 tokenizer 门槛；缺少依赖时应该失败，而不是静默跳过。宿主 OpenSSH、bubblewrap、SMTP/TLS 与物理控制台还须按部署验收清单单独验证。

新增默认响应字段应说明必要性并更新 token / 字节预算。不要把全部内部 dataclass 直接序列化成每次响应。
