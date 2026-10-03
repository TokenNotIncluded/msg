# 新 Root：旧内容、Git 与历史身份导入

本页用于全新 v4 安装。旧服务保持运行，目标使用全新 PostgreSQL 数据库、配置和数据目录。旧 Root、CA、凭据、托管私钥和外部任务均不启用。`legacy_rehearsal` / `legacy_git_rehearsal` 的测试 Root、测试 holder、映射和目标库不能用于生产。

## 1. 准备材料（可自动执行）

先完成 DEPLOYMENT.md 的候选安装、Python 3.15、PostgreSQL、文件权限和回滚检查。配置最终服务 URL，但暂不接公网流量。新库不能指向旧库或预演库；libpq service 文件由对应执行用户读取，连接密码不放命令行。尚未切流时，公网地址仍是旧服务，不能用它登记新身份。

迁移材料放在受保护目录（目录 0700、文件 0600）；生成审批草稿、签名和导入时使用同一个文件拥有者，或者通过明确的受保护复制交接拥有权。Root 控制台必须能读取草稿，服务执行用户必须能读取实际导入材料。不要放入 Git 或 /tmp。

| 材料 | 校验/用途 |
| --- | --- |
| `offline-legacy-0221.sqlite` | SHA-256 `ef550cbe7126cd265268b7f1da69ce6db5a65dccada5d0720028204cf9885389`，只读冻结源 |
| `legacy-repository.bundle` | SHA-256 `a89b4e6bc3d98c4f3ea1324ac70bd4b875ba1d783ecf973d695e86ac1fa257a9` |
| `legacy-repository-manifest.json` | 原始固定 refs、默认 ref 和摘要，保持私有 |
| 旧 Root **公钥**（如有） | 仅验证历史关系，不导入旧私钥，不赋予权限 |
| 新 holder 的客户端目录 | 新生成并持久保存的真实身份密钥；不能使用预演目录 |

这些是已有冻结副本；最终切流前需要重新冻结/核对旧服务新增数据，旧服务仍写入时不得声称此副本是最终最新状态。源库和 bundle 校验由导入器再次执行。

下文 `CFG`、`MATERIALS`、`CLIENT`、`HOLDER`、三个 `*_PARENT` 都由部署记录明确赋值。`CFG` 是候选配置目录；`MATERIALS` 是上述受保护目录。不要将示例变量直接当成生产路径。

## 2. 新 Root 与在线 CA

默认由操作系统 root 在物理、KVM 或串口控制台执行。`init` 与 `cert issue` 另支持经明确授权的 OS-root SSH 交互终端和显式 `--allow-ssh`；仍要求交互 PIN。下文旧内容/Git/身份迁移批准的签署仍仅限物理控制台，不能套用此选项。PIN 只在 `getpass` 提示时输入，不通过聊天、参数、环境变量或管道传递。

```sh
msgd --config-dir "$CFG" init --data-dir "$NEW_DATA" --service-url https://msg.lmm.best
msgd --config-dir "$CFG" cert issue "$ONLINE_CA_REQUEST"
msgd --config-dir "$CFG" doctor
msgd --config-dir "$CFG" selftest
```

`ONLINE_CA_REQUEST` 使用 init 返回的 CSR ID，检查并输入 CLI 展示的确切 CSR 摘要。init 生成全新 Root；cert issue 单独批准新在线 CA。旧在线 CA 不参与。

## 3. 登记真实 holder，创建三个私有目录

通过候选实例的正常 `identity.register@2`、`content.topic_create`、`content.chmod` 操作建立真实身份与三个独立的 `0700` topic：内容、Git、历史身份。holder 是保存迁移数据的新拥有者，不等于任何旧作者。内容和身份导入要求各自 topic 为空；批准绑定 topic 当前 generation。

切流前可使用下面的本地入口；它通过正常 HTTP 路由、签名客户端和权限检查，完全不发送网络请求。以最终持有客户端目录的操作用户执行，并给予该用户明确的候选配置/数据库访问；不要广泛放开服务器文件权限。`CLIENT` 必须是新建的专用目录，登记失败可用同一目录重试，已有登记不能重复执行。不要导入旧客户端目录。

```python
# 保存为受保护的本地操作脚本；从环境读取路径，不读取或输出密钥。
import asyncio, os
from pathlib import Path
import httpx
from msg.application import Application
from msg.config import load_settings
from msg.client import ClientState, MsgClient
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


async def main():
    app = Application(load_settings(Path(os.environ['CFG'])))
    try:
        await app.load()
        state = ClientState(Path(os.environ['CLIENT']), server=app.settings.service_url)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
        ) as http:
            client = MsgClient(state, HTTPTransport(state.server, http=http))
            registered = await client.register(os.environ['NEW_HANDLE'])
            assert registered.status == 'ok', registered.error
            print('HOLDER', state.subject)
            for name in ('legacy-content', 'legacy-git', 'legacy-identities'):
                result = await client.call(
                    'content.topic_create', {'parent': '/main', 'name': name}
                )
                assert result.status == 'ok', result.error
                ref = result.resources[0]
                async with app.metadata.transaction(write=False) as tx:
                    generation = (await tx.resource(ref.id)).generation
                result = await client.call(
                    'content.chmod',
                    {'id': ref.id, 'mode': '0700'},
                    expected=((ref.id, generation),),
                )
                assert result.status == 'ok', result.error
                print(name, ref.id)
    finally:
        await app.close()


asyncio.run(main())
```

检查新身份登记和三个 topic 的实际结果，再记录 `HOLDER`、`CONTENT_PARENT`、`GIT_PARENT`、`IDENTITY_PARENT`。不要从日志推测未完成步骤；脚本部分失败后通过现存 holder 正常客户端逐项补齐，不删除持久身份密钥重新注册。

## 4. 生成计划与审批草稿（可自动执行）

```sh
python -m msg.storage.legacy_identity_plan --snapshot "$MATERIALS/offline-legacy-0221.sqlite" --sha256 "$SQLITE_SHA256" --output "$MATERIALS/identity-plan.json"
python -m msg.admin.legacy_approval --config-dir "$CFG" prepare-content --snapshot "$MATERIALS/offline-legacy-0221.sqlite" --sha256 "$SQLITE_SHA256" --operator "$HOLDER" --parent "$CONTENT_PARENT" --unmapped --output "$MATERIALS/content-draft.json"
python -m msg.admin.legacy_approval --config-dir "$CFG" prepare-git --bundle "$MATERIALS/legacy-repository.bundle" --manifest "$MATERIALS/legacy-repository-manifest.json" --operator "$HOLDER" --parent "$GIT_PARENT" --name legacy-repository --output "$MATERIALS/git-draft.json"
python -m msg.admin.legacy_approval --config-dir "$CFG" prepare-identities --plan "$MATERIALS/identity-plan.json" --operator "$HOLDER" --parent "$IDENTITY_PARENT" --unmapped --output "$MATERIALS/identity-draft.json"
```

`SQLITE_SHA256` 使用材料表的实际摘要。若有旧 Root 公钥，第一条命令额外指定 `--root-public-key` 公钥文件，增强历史校验；缺少它时保留计划中的证据不足状态，不能称已验证旧 Root 链。三份草稿有效期两小时；过期或 parent generation 改变后重新准备并重新批准。

`--unmapped` 将每个旧作者/身份明确映射为 JSON null，保留旧 ID、历史签名和验证结果，不声称旧作者就是新 holder。未映射作者不会获得登录或资源权限。将来认领必须独立验证并正常登记；此归档不能当登录凭据。只有完成相应验证后才使用私有 `--mappings` 文件显式关联已注册的新 Subject。

## 5. 审核并签署三份批准（必须本机控制台）

```sh
python -m msg.admin.legacy_approval --config-dir "$CFG" sign --draft "$MATERIALS/content-draft.json" --output "$MATERIALS/content-approved.json"
python -m msg.admin.legacy_approval --config-dir "$CFG" sign --draft "$MATERIALS/git-draft.json" --output "$MATERIALS/git-approved.json"
python -m msg.admin.legacy_approval --config-dir "$CFG" sign --draft "$MATERIALS/identity-draft.json" --output "$MATERIALS/identity-approved.json"
```

每次显示实际 holder ID/handle、目标私有路径与 generation、新 Root 公钥摘要、映射/未映射数量、确切审批摘要。Root 操作者核对真实新身份，输入确切确认短语，再本机输入 PIN。helper 只接受这三种固定格式，拒绝任意 purpose 或额外字段。不会自动批准任意旧身份，也不会启用旧队列。

## 6. 实际导入（有签名后可自动执行）

```sh
python -m msg.storage.legacy_resource_import --config "$CFG" --snapshot "$MATERIALS/offline-legacy-0221.sqlite" --signed-approval "$MATERIALS/content-approved.json"
python -m msg.storage.legacy_git_import --config "$CFG" --bundle "$MATERIALS/legacy-repository.bundle" --signed-approval "$MATERIALS/git-approved.json"
python -m msg.admin.legacy_approval --config-dir "$CFG" apply-identities --plan "$MATERIALS/identity-plan.json" --signed-approval "$MATERIALS/identity-approved.json"
```

将输出重定向至受保护的证据文件；完整导入报告可能含旧路径、ID 或 refs，不粘贴到公开日志。导入器再次验证 Root 签名、服务、期限、holder 和 generation。三个导入各自事务提交；失败时先检查相应持久报告，不重复已完成导入，也不为了重跑而删除源材料。

## 7. 验收与切流边界

核对源摘要未变、行数/对象数量、内容原文/附件、Git OID/ref、私有历史签名来源和身份验证状态。用真实 holder 的签名请求验证读取，用匿名请求确认拒绝。旧 URL 只在 `/_legacy/<SQLite SHA-256>/...` 命名空间适配，当前目标权限通过后才跳转；没有无条件公开旧 URL。

确认无旧凭据、CA、托管私钥、权限或外部队列被激活。身份归档仅私有文件，不创建可登录旧 Subject。默认所有迁移内容保持私有；公开、授权、域名切流及旧备份退役是另外的显式操作。三项导入通过不等于部署验收全部通过；保留旧服务和回滚材料，完成最终冻结一致性及部署验收后再切换。

## 8. 恢复原公开阅读范围（单独审核，不能省略）

默认私有是导入和核验阶段的保护措施，不是全站升级的最终可见性。旧 schema 的 `boards.locked` 是写入锁，不是私密标记，不能据此推导访问权限。固定旧版本的普通 board/live post/attachment GET 为匿名读取；`archived_posts` / `archived_attachments` 不属于当前公开内容集合。仅从 SQLite 不能证明反向代理、站点级限制或 Git 的访问规则，因此这些外围限制需要部署清单复核；分类缺证据时保持待审核，不能自动公开。

应用级依据固定源码 `e12eea32764656eec82452fee111e5b4fb6d8a42` 的 `server.py`：`do_GET → _dispatch → _route` 没有全局读取认证，`/file` GET 直接取附件。分类依据这一读取路径和实际 schema，不是“live 就公开”。部署/反代 ACL 未核对的条目属于未知，保持私有。审批批次按以下集合从**已验证冻结快照**导出，不扫描正文或密钥：

| 集合 | 快照查询 | 经审核的目标模式 |
| --- | --- | --- |
| 公开候选板块 | `SELECT name FROM boards ORDER BY name` | 0755，仅恢复阅读/遍历；不开放写入/加入 |
| 公开候选在版帖 | `SELECT id FROM posts ORDER BY id` | 0644 |
| 公开候选在版附件 | `SELECT a.id FROM attachments a JOIN posts p ON p.id=a.post_id ORDER BY a.id` | 0644 |
| 历史归档及历史签名文件 | archived_*、各 provenance | 保持 0600，不随当前帖一并公开 |
| 历史身份、凭据记录、Git | 独立迁移 topic/仓库 | 保持私有；Git 另查原部署规则并单独批准 |

原锁定板块与未锁定板块都不自动恢复写权限。以下本地只读脚本生成具体的候选 `resource ID / generation / mode` 批次文件，输出只含数量和摘要。它不执行 chmod；完整 plan 保持私有并交新 Root 操作者审核。`PUBLICATION_PLAN` 必须是不存在的新文件。

```python
import asyncio, hashlib, os, sqlite3
from pathlib import Path
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import digest
from msg.admin.legacy_approval import write_new


async def main():
    snapshot = Path(os.environ['MATERIALS']) / 'offline-legacy-0221.sqlite'
    sha = os.environ['SQLITE_SHA256']
    with snapshot.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == sha
    with sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        candidates = [
            (table, row[0], mode)
            for table, query, mode in (
                ('posts', 'SELECT id FROM posts ORDER BY id', '0644'),
                (
                    'attachments',
                    'SELECT a.id FROM attachments a JOIN posts p ON p.id=a.post_id ORDER BY a.id',
                    '0644',
                ),
                ('boards', 'SELECT name FROM boards ORDER BY name', '0755'),
            )
            for row in db.execute(query)
        ]
    app = Application(load_settings(Path(os.environ['CFG'])))
    try:
        await app.load()
        async with app.metadata.transaction(write=False) as tx:
            report = tx.setting('legacy-import:' + sha)
            assert report and report['source_sha256'] == sha
            entries = []
            for table, old_id, mode in candidates:
                rid = 'legacy_' + sha[:24] + '_' + digest((table, old_id))[7:31]
                resource = await tx.resource(rid)
                assert resource.owner == os.environ['HOLDER'] and resource.state == 'active'
                entries.append({
                    'id': rid,
                    'generation': resource.generation,
                    'mode': mode,
                    'source_table': table,
                })
            parent = await tx.resource(os.environ['CONTENT_PARENT'])
            assert parent.owner == os.environ['HOLDER'] and parent.mode == 0o700
            for item in entries:
                assert parent.id in {r.id for r in await tx.ancestors(item['id'])}
            entries.append({
                'id': parent.id,
                'generation': parent.generation,
                'mode': '0755',
                'source_table': 'publication_parent_last',
            })
        plan = {
            'source_sha256': sha,
            'holder': os.environ['HOLDER'],
            'entries': entries,
            'restores': 'read-only-public-candidates',
            'review_required': True,
            'source_code_commit': 'e12eea32764656eec82452fee111e5b4fb6d8a42',
            'classification': 'application-anonymous-read-candidate',
            'deployment_acl_review': None,
        }
        write_new(Path(os.environ['PUBLICATION_PLAN']), plan)
        print({'candidate_count': len(entries) - 1, 'plan_digest': digest(plan)})
    finally:
        await app.close()


asyncio.run(main())
```

审核记录应包含 plan 摘要、已确认的旧服务/反向代理匿名阅读规则，以及明确批准或排除的条目。现有迁移批准仅批准私有导入，不能作为公开批准使用；当前没有专用 Root 可见性批次签名命令，不能用三种导入签名 purpose 冒充。新 Root 操作者完成这一步审核后，由真实 holder 使用正常 `content.chmod`，每条严格使用批准 plan 的 ID、mode、generation：

```sh
msg --config-dir "$CLIENT" --server https://msg.lmm.best call content.chmod '{"id":"<plan中的ID>","mode":"<plan中的mode>"}' --expect '<同一ID>=<plan中的generation>'
```

切流前使用第 3 节的本地 HTTP 客户端调用相同 `client.call('content.chmod', {'id':item['id'],'mode':item['mode']}, expected=((item['id'],item['generation']),))`，不调用仍指向旧服务的公网 URL。先应用在版帖/附件，再板块，最后才将 `CONTENT_PARENT` 改为 0755；任何条目失败立即停止，保持父 topic 私有，不跳过 generation 冲突。该批次不是单事务：若中断，读取实际模式/generation重新生成并审核剩余计划，不盲目重跑。最终父 topic 公开后，用匿名真实 HTTP 请求验证恢复的普通内容和附件可读、归档/历史签名/身份材料仍拒绝，才算可见性恢复完成。
