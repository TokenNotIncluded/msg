# 迭代路线与验收出口

权威[项目设计](https://docs.google.com/document/d/1EM5Qr5qdg6tAFi2wvY0EBm6zxMj6DTBMc_dybU5qkz0/edit)最新修订仍为2026-09-27T05:54:08.096Z。

**当前状态：PR #63已合并main，merge commit `2b4d483d58dfe4eb2d81565377238dbb5e17a6b5`；本地414 passed、8 conformance、build通过，[main CI 36302710481](https://github.com/TokenNotIncluded/msg.lmm.best/actions/runs/36302710481)completed/success（414 core、8 conformance、build），未部署。** patch/rebase/batch与ShareGrant@2已合并，不改旧@1；Git不可达孤儿、历史generation映射及完整分享/编辑契约仍有缺口，不能称全部需求完成。

1. **c62e516的309/8/build与CI已通过；验证后续CLI Search/Grep和SyncCursor改动。** 保持未接custodial写fail-closed；网络代解密不开放。继续补完整RouteSpec/只读零业务变更矩阵，UA guard不替代认证。
2. **补齐custodial非空库存升级。** 双钥PoP/本地journal/空库存切换与新钥查结果已有；下一步显式逐对象可恢复密文rewrap→确认迁移结果→撤销托管token→按策略销毁旧vault钥并审计。每步断线/重复请求可安全恢复，未完成不能冒称self-custody。严格token一次展示必须同时解决首次响应丢失恢复，不靠重复返回秘密掩盖。
3. **扩展Sync和QueryRef边界。** 当前Sync仅15分钟/64个seen引用，补超限明确行为、权限新增旧事件回补、跨协议离线恢复；撤权只最小失效已知引用。QueryRef过期+1h条件回收已有，继续验证引用/并发/重启，补token-only纯路径；所有维护变更仍只在维护任务，不挪到GET。
4. **完善Recovery与Policy生命周期。** 当前自托管opt-in/精确Envelope/客户端OR演练及选定条目rewrap已有；服务器不能证明recipient集合。完整账号恢复、Policy UI、历史钥迁移及Legacy登记已有工作树切片，执行仍独立交付，不把备份密文当账号授权。
5. **补规则、内容与UI。** source/RuleSet精确映射、规则删除迁移、客户端Revision manifest独立签名、requires_rules精度；Notes完整生命周期/Todos、HTML/TUI/搜索LinkSet、完整组分享/patch/grep、通知、其他Agent原语、同域hosting与大包Git/LFS和真实宿主验收。

每项补默认/样例/doctor/selftest/启用配置CI。阶段性的295/8不等于完整feature，也不代表发布、部署或生产迁移。

本批同域hosting已提供匿名只读/禁JS sandbox安全切片；下一轮先把root样例变为真正Resource/Revision并补preview，再在明确需求下评估是否开启JS。浏览器分别验证脚本阻止与实际发起请求的服务端拒绝，不能以opaque探针通过代替产品allow-scripts验收。保持危险格式附件、所有投影同等隔离和完整发布/回滚验证。

SearchQuery共享授权投影/纯路径及长查询、已知范围Grep和Legacy本人签名登记/更新/归档切片已有，但实际执行遗愿留在完整恢复/授权审计之后。搜索须先鉴权再参与计数/聚合/排序/补全，Grep复用当前授权并固定Revision，不能因count_only不返回正文而省略授权。LegacyDirective不产生任何新权限、不依presence或超时自动触发，签署愿望不等于未来执行权。

当前新增查询/遗言切片本地验证通过，后续保持以下回归并补完整feature：搜索与grep测试私有正文/计数/snippet/LinkSet无泄露、QueryRef撤权/过期、排序更新分页及范围外资源对预算/时序影响；已修scope候选与过滤后计数并通过2001范围外回归；更广时序安全仍不宣称完成。Legacy只做声明，不实现自动执行；验本人写、旧revision冲突、公开表示不带私有引用、归档后读取和所有普通互动旁路。保留受限正则和明确输出上限，不为“高级搜索”引入无边界表达式或自动workflow。

Legacy当前限制：已有私有历史的遗言不能切换为公开（legacy_private_history_cannot_be_published），避免通用discovery.get/raw借当前公开mode暴露历史Revision；legacy_get另按所选版本visibility校验。不是逐版本公开发布机制，不能将该限制描述为支持安全公开旧私有历史。

后续CLI按受限单页与显式cursor验收；Sync仍有64引用上限，授权变化要求resync而非宣称自动回补。分别测试数据内resync_required和HTTP超长路径413，未提交改动不沿用c62e516 CI。

## 当前未提交token @2交付边界

identity.temporary/custodial_create/token_create/token_rotate新增@2，要求独立于nonce的至少32字节恢复材料，恢复窗口最多15分钟且不超过原凭据期限。业务提交仅存verifier和绑定事实，response hook通过持久原子claim最多返回一次token；已claim的相同请求返回token_delivery_unavailable。claim提交后丢响应通过identity.token_recover显式换新token，旧token撤销、旧恢复材料单次消费；新凭据保持原ceiling及expires_at，并绑定新的独立恢复材料。服务器不承诺网络恰好送达一次。

该严格模式目前是选择@2才启用，旧@1仍可重放交付；标准客户端当前已默认切换@2；不能宣称全平台token一次展示已经完成。token定向20 passed包含竞态、丢响应/恢复、重启和过期等切片，已纳入319项全套，不额外累加；仍无本批CI。

CLI search/grep为受限单页、显式cursor，相关CLI/Sync定向11 passed。Sync v2在授权epoch/Topic成员摘要变化时，只能返回已知撤权最小ID+resync_required且无续cursor，其余要求resync；>64引用明确失败，不静默淘汰。完整signed proof嵌入URL时，即使50 seen也可能触发路径413，可用既有header承载proof；这意味着全量纯路径体验仍有缺口，不宣称只靠路径可支持所有窗口。

## 当前规则迁移与客户端安全边界

规则源按稳定rule_id识别，source_paths与显式old→new迁移声明控制移动；保持Resource ID/历史，逐文件digest/version同步。未知/重复rule_id、未声明移位、缺源/删除、悬空requires_rules均fail-closed；文件清单先完整校验再写，重复load不重复Revision。该切片不是任意规则删除/退休机制或完整规则全文迁移。

标准客户端当前默认使用token发行@2，发送前原子保存0600本地journal及独立恢复材料；丢响应保留journal，显式msg identity recover-token恢复，不自动降级@1。含秘密请求仅允许HTTP/GraphQL/MCP HTTP的body传输，PathGET拒绝；真实域必须HTTPS，仅testserver/localhost/127.0.0.1/::1例外。light.local不属于此例外，历史light.local HTTP证据仅为非秘密本地读取探针，不能作为token发行/恢复上线证明。日志脱敏与TLS部署仍须验证。

当前326/8/build仅本地，未提交/无对应CI。公开发布仍缺长期Sync（64引用/15分钟、权限变化resync及路径长度边界）、非空托管库存通用迁移/恢复、完整hosting preview/JS/root Resource与宿主矩阵、完整feature默认/doctor/selftest；不能用新客户端默认@2宣称旧@1已消失或整个服务全部完成。

## 当前第四批实现与验收限制

真实/@root/web已有website/部署清单/文件Resource及Revision，不再只有代码响应样例；hosting.preview创建private候选、不切active指针。读取preview必须携匹配discovery.raw的签名header，不能把返回URL当可直接无凭据浏览器导航；保持禁JS sandbox。hosting切片已纳入332项全套，但不等于完整浏览器/部署矩阵。

Notes已有专用archive/restore，Todo已有本人私有创建/更新/读/列表/归档/恢复，默认pending/neutral；notes定向4 passed。第五批已增加到期本人Inbox维护投递与去重，完整feature仍待最终验收。

ShareGrant工作树已有直接叶资源限时read/revoke/list，合并定向验证通过；不宣称组接收者、转授链或ShareLink完成。Sync仅补>64重放失败/GET零业务状态回归，定向1 passed；持久checkpoint/ack已有第六批切片，无限容量同步未实现，不能以失败回归称为扩容。

第四批已完成合并定向：`.venv/bin/python -m pytest -q tests/test_share_grants.py tests/test_notes_todos.py tests/test_hosting_same_origin.py tests/test_sync_cursor.py tests/test_dictionary.py` 为 **28 passed**；短码snapshot由162增至173项，旧码意义不变。此前各项定向不再累加。本批最终332/8/build通过，未提交、无对应CI。

ShareGrant为直接叶资源限时read/revoke/list；私有Note可单项分享但不授父目录列举。SOUL、Todo、DM、system-managed及preview均排除，不等于组分享/转授链/ShareLink。旧安装Online CA证书的grants是冻结快照，新增sharing.basic不能自动扩入旧证书；启用前需受控重签并验证当前授权范围，不能以新安装测试代替存量迁移。

## 第五批局部实现与边界

Todo due由显式维护任务投递，仅本人Inbox且去重，定向8 passed；普通GET不发提醒，不外发给其他主体。custodial单条age rewrap定向11 passed，仅处理明确选择的条目；旧vault和token保留，结果client_decryption_verified=false，不因服务器产出新密文就自动完成升级或销毁旧钥。

Git /-/receive-pack独立32MiB硬上限、每worker最多2并发、上传120秒deadline；staging流式SHA256、64KiB分块feed，定向10 passed。它不是完整Git/LFS交付：LFS已有第六批最小切片但完整验收未做，多ref原子性尚无证明，跨worker磁盘配额未做；每worker限流不等于全部署统一配额。当前336/8/build本地通过，尚未提交/无本批CI，不沿用d4affbb结果。

## 第六批持久Sync与LFS局部实现

communication.sync_checkpoint_open/ack为签名/-/写，持久checkpoint用CAS版本更新；GET只计算pending与ACK描述，不推进已提交状态。seen精确记录最多10000引用，定向10 passed。它与旧64引用短cursor并存，不是无限容量或自动已读；checkpoint ACK仅同步进度确认，不是内容ACK。超过上限、并发ACK、旧版本及撤权仍需明确失败/重同步，完整生命周期与长期部署验收尚缺。

LFS最小上传/下载切片定向8 passed：普通repo路径仅download，上传仅/-/；SHA256/size校验后原子发布。尚未验证真实git-lfs/Range已有第七批证据，跨worker配额尚缺，也未共用Files/Transfer的BlobStore；不能称完整Git/LFS feature完成。SHA256不是读取权限，普通路径不得签发上传或隐式发布。

托管冻结清单/映射/新钥签名ACK已有，但历史Revision依赖旧vault、finalize_ready=false，完整升级仍未完成；本批本地345/8/build通过，仍未提交/无本批CI；c965385远端失败不能作为通过证据。

## 第六批收口状态与CI回归

Sync checkpoint签名open/ack、GET pending只读、CAS及seen<=10000已有；LFS basic upload/download与签名PUT已有。托管升级已冻结迁移清单、记录映射并接受新钥签名ACK，但历史Revision仍依赖旧vault，finalize_ready=false；旧vault/token不能据此销毁，不称完整升级完成。

c965385 CI36293461291失败源于Git默认1MiB postBuffer的0000探测请求占用相同request_id，后续真实包409。当前修复将该probe限定为只读鉴权、不创建job/幂等业务结果；真实包仍按原授权/摘要/幂等执行。GIT_CONFIG_GLOBAL=/dev/null联合定向27 passed，短码175→183旧义保留。本批本地345/8/build通过，仍未提交/无本批CI；真实git-lfs/Range已有第七批证据，跨worker配额与共用BlobStore仍缺。

## 第七批局部实现与证据

真实git-lfs 3.8.0使用1.3MB对象完成push/clone/pull；LFS Range及/-/.git兼容已有，联合定向24已纳入最终351项全套。跨worker配额、与Files/Transfer共用BlobStore及GC/引用生命周期仍待完成，不能据单对象成功宣称全量LFS运维完备。

ShareLink默认off，system.share_links_set为受控开关；token仅POST body，长短GET token路径均已拒绝。它不是裸URL可直接浏览器打开的分享能力；仍须当前授权/期限/撤销边界，不把token写入普通页面、日志或可点击执行URL。

托管历史age Revision迁移已有显式私有新钥副本与mapping，旧原文/历史不改写；finalize仍fail-closed，不因副本存在就销毁旧vault或撤销最后入口。外部密文和实际recipient集合无法由服务器证明，完整升级仍缺。这些增量本地351/8/build已通过，仍未提交/无本批CI；历史密文仅显式副本mapping，finalize仍关闭，不借用8480589的CI。

## 第八批facets与权限快照边界

lexical_search@1保持已发布schema，facets只进入@2；QueryRef、续页、HTTP query与q/2共用@2契约，不能将新字段偷偷加入@1。短码190→191，旧码意义保留；本地355/8/build通过，尚无本批CI。

doctor.authority_snapshot只读比较当前Registry与旧Root/Online CA签名grants快照，不修改证书、不自动扩权。旧签名快照不能原位安全增加能力；需要的新增授权必须显式本机Root流程处理，若需Root轮换会使旧信任链失效，必须先评估迁移/重签影响。诊断结果不是升级生产授权的许可，本批不自动修改生产。

## 第九批当前切片

SearchQuery@3新增source_kind/relation_type过滤，HTTP q/3、query-string、QueryRef/续页使用同版本，旧@1/@2不改义；关系条件只匹配当前Revision关系类型，不等于任意图查询或全套高级搜索，suggest已有第十批@4显式切片，spell仍缺。

LFS新对象与Files/Transfer共用blob_dir CAS，repo hardlink作为GC保留根，修复显式pin误删。PostgreSQL串行准入默认4GiB并用共享卷sentinel核对后端一致性；该限制只覆盖LFS新对象，不是所有文件/全worker staging或整个部署磁盘配额。旧repo LFS迁移、全部署staging及其它CAS写入预算仍缺，不能以共用目录推断所有历史对象已迁移。

CLI已有hosting preview/deploy/activate/history；private preview使用签名header，输出遵守惰性文件创建/覆盖限制，不提供无凭据可打开preview URL。当前本地363/8/build通过，未提交/无本批CI；真实浏览器全部入口矩阵、托管JS、长期运维与完整feature门槛不由此自动完成。

第九批边界：Files/LFS/Transfer共用BlobStore对新LFS对象已实现，旧对象与全部署staging/其它CAS写预算尚未完成；hosting preview CLI不输出裸可执行URL，签名header与惰性文件创建限制保留。

## 第十批局部实现与未验范围

Webhook仅Inbox-based显式opt-in，secret由vault封存；HMAC签名、投递去重、SSRF限制与uncertain有定向并纳入本地全套。真实公网接收端/完整网络部署矩阵及Domain Event已有受限owner订阅切片，公网未验，不能声称外部投递端到端已完成；默认关闭和当前授权裁剪不放宽。

TUI仅第一片只读Home/Inbox/Search/Thread，复用公共客户端契约且不自动ACK；没有完整产品功能、写交互或全部终端/恢复矩阵。Search@4的suggest为显式请求、不默认改写查询；spell未实现，旧版本schema与短码不改义。

当前379/8/build只是未提交本地证据，短码192→196；不借用e18d0b6的363/8成功CI，不表示生产部署。

## 第十一批备份、文本patch与Domain Event切片

backup v4验证PostgreSQL/Git/CAS/LFS引用，恢复仅接受v4，隔离restore带写暂停与worker/daemon禁外发marker，必须显式人工提升后才作为运行实例；root秘密单独备份。生产在线备份未演练，外部Git写入可能使一致性检查fail-closed，不能宣称任意在线负载下无中断备份。

content.text_patch支持exact/context唯一匹配，正文上限1MiB，歧义拒绝；尚无安全rebase或atomic batch。Domain Event Webhook仅支持post_create/reply/post_edit，需owner显式订阅；这不是任意全站事件授权，真实公网发送/接收矩阵仍未验。

## 05:54权威修订复核

已完整实时读取ChatGPT文件夹唯一项目设计，修改时间2026-09-27T05:54:08.096Z，共185段、01–15章。相较00:35版主要压缩重复描述；未据段数减少认定功能删除。第15章明确第01–14章每条字段、默认、允许/禁止、状态转换、路径/别名/表示/查询均需逐项测试，完成仍须实现/默认/样例/doctor/selftest/CI，优先级仍以权限、零副作用、一致性与恢复为先。

本批392/8/build只证明当前局部代码。backup v4及recovery-drill硬闸是本地实现选择，权威要求是一致数据库快照/引用内容验证与根秘密另备，并未指定v4兼容版本。生产在线备份/外部Git并发仍未演练。text_patch只覆盖exact/context唯一匹配、1MiB；完整file/post命名契约、unified/heading/block patch、rebase/batch仍缺。Domain Event仍要求capability：当前owner签名/manage约束仅是受限切片，独立webhook.domain capability及无权/撤销回归已补，完整公网矩阵仍待验收，公网未验。

本批Domain Event Webhook已加独立webhook.domain capability，Basic OnlineIssuer普通issue_grants白名单不含该能力；订阅及每次投递复核owner/ACL与当前证书，无cap拒绝、证书撤销后停止投递。当前仅post_create/reply/post_edit，公网端到端仍未验。备份v4仅接v4、恢复drill写/worker硬闸及text_patch exact/context局部边界不变；392/8/build为未提交本地证据，无本批CI。

## 第十二批治理与验收映射切片

BootstrapManifest v5提供12个feature rows及真实doctor/selftest映射；disabled/partial是完成度报告，不关闭现有API，也不意味着整套第15章TDD矩阵已完成。新增feature必须继续补确定默认、样例、正常/拒绝/并发/恢复与CI证据。

Organization已有open/approval/invite/managed四策略、owner/maintainer/member角色、旧组织兼容与/&public虚拟成员；默认invite，不直接授予未确认成员权限。完整跨入口、转授/分享、退组与边界组合仍按feature验收，不用角色名推导资源/CA权。

post metadata/rollback及post_write/patch alias已有局部切片，旧契约短码不改义；rebase、atomic batch和完整file/post操作族仍缺，rollback也不能绕过当前权限/版本前置条件。本地408/8/build不等于完整产品或生产交付。
