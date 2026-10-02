# 旧项目退役范围与验收（#220）

本页用于确认旧 **lmm-api / cortexfs** 的准确退役对象。当前 MSG、Root 材料和
非目标项目必须保留；共享 PostgreSQL、Valkey、Nginx 不属于退役对象。
本页没有执行停服、卸载、卸载挂载或删除，也不证明 #220 已完成。

项目退役与 [MSG 路径迁移](FILESYSTEM_LAYOUT.md)、
[MSG 部署回滚](DEPLOYMENT.md)、[备份密钥退役](CUSTODIAL_HISTORY.md) 分别验收。
这些文档中的“旧路径”“旧服务”不能作为 lmm-api / cortexfs 的删除清单。
`scripts/audit_filesystem_paths.py` 扫描源码调用点，不检查服务器残留。

## 先固定准确范围

为每台目标主机记录一份私有清单，保留采集时间和主机标识。每个对象单独列一行：

| 字段 | 必须记录的内容 |
| --- | --- |
| 项目与归属 | lmm-api 或 cortexfs；包归属、部署记录或其他实际依据。名称相似仅是候选。 |
| 对象 | 确切包名；完整 systemd unit 名；挂载目标；绝对配置、数据或归档路径；专属数据库/角色、缓存键空间或代理引用。 |
| 当前状态 | 安装/运行/启用/挂载状态、所有者、大小；无法读取写“未知”，不能写“没有”。 |
| 引用与依赖 | 启动入口、依赖它的服务、代理 upstream、共享数据库/缓存消费者、打开的文件及挂载。 |
| 拟执行动作 | 保留、停用、移除或归档；明确是否包含数据，不能由“卸载包”推导。 |
| 回滚与保留 | 可验证的备份位置、恢复方法、保留原因和期限；已有归档不得仅因名字带日期而删除。 |
| 批准与验收 | 对准确清单的批准时间、执行记录、操作后的复查结果；未执行的对象保留待办状态。 |

另列保护清单：当前 MSG 的所有实例、实际配置/状态/托管目录、Root/CA 与客户端身份
材料、正在使用的备份，以及其他项目和共享基础设施。MSG 的目录可能经过迁移，
不能只保护某一套默认路径。不要把私有清单、密钥位置、连接串或原始配置贴到 issue。

范围存在以下情况时，先澄清或保留该对象：

- 通配符、目录名推测、`..`、未解析的符号链接，或准备删除目标项目的公共父目录。
- 目标目录包含保护项，或目标落在保护目录中；一个可共享的父目录不等于整个目录可删。
- 挂载指向另一份数据，子挂载仍在使用，或同一数据通过 bind mount/别名继续被引用。
- 一个包、服务、数据库、角色、Valkey 实例或 Nginx 配置仍服务于其他项目。
- 依赖、归属、备份可恢复性或保留期限尚未确认。

## 只读盘点

以下是候选发现，不是批准清单。远程执行前先核对目标主机；全部命令仅用于读取。
不要使用 `nginx -T`、`systemctl cat`、日志导出或环境导出来生成公开报告，它们可能
包含秘密。权限不足、命令失败或不支持的系统必须记录为检查未完成。

```sh
hostname
pacman -Q | rg '^(lmm-api[^ ]*|cortexfs[^ ]*) '
systemctl list-unit-files --no-pager --no-legend 'lmm-api*' 'cortexfs*'
systemctl list-units --all --no-pager --no-legend 'lmm-api*' 'cortexfs*'
findmnt --json --output TARGET,FSTYPE
rg -l --glob '*.conf' 'lmm-api|cortexfs' /etc/nginx
```

包和服务可能改过名字，仍须按部署记录核对；零匹配不能证明退役完成。
包检查适用于 Arch 的 pacman，其他系统改用它自己的包管理器只读查询。
`rg -l` 只列文件名，不展示匹配的配置值。挂载输出刻意不包含可能带凭据的 source。

对清单中已确认的单个对象继续只读核对，不把候选列表直接送入删除命令：

```sh
retirement_unit='完整且已核对的.service名称'
systemctl show "$retirement_unit" --no-pager \
  -p Id -p ActiveState -p SubState -p UnitFileState -p FragmentPath \
  -p Requires -p Wants -p BindsTo -p PartOf -p Triggers -p TriggeredBy
systemctl list-dependencies --reverse --plain --no-pager "$retirement_unit"

retirement_path='/已核对的绝对路径'
stat -c '%F %U:%G %s %n' -- "$retirement_path"
readlink -e -- "$retirement_path"
findmnt --target "$retirement_path" --output TARGET,FSTYPE
du -sx -- "$retirement_path"
pacman -Qo -- "$retirement_path"
```

`findmnt --target` 返回覆盖该路径的文件系统，不表示该路径本身是挂载点；仍需核对
全量挂载列表中的目标和子挂载。不存在的路径与无权限解析的路径须分别记录。
包没有拥有数据目录也不能证明它可删除。数据库、缓存和代理另按受授权连接核对
专属对象及消费者，只保留名称、归属、尺寸和引用结论，不导出数据或凭据。

## 获准执行与实际验收

批准必须针对准确清单和数据保留决定，不能从“部署 MSG”“修改 MSG 路径”或
“迭代项目”推导为退役其他项目。批准后按依赖顺序处理启动入口、服务、挂载、
代理和专属数据，再移除确定不共享的包与路径；共享服务只移除已确认的专属引用。
保留操作前证据与回滚材料，普通停服或路径不存在都不能替代全部验收。

每个检查记录采集时间、主机、确切对象、期望、观察值和通过/失败/未知：

| 验收面 | 实际证据 |
| --- | --- |
| 包 | 目标包不再安装；卸载范围没有移除 MSG 或其他项目依赖。 |
| 服务及激活入口 | 目标 unit、socket/timer/path、用户服务、容器或 cron 等入口按批准范围移除；不再运行或自动启动。仅 stopped/disabled 不等于移除。 |
| 挂载 | 内核挂载及 fstab/systemd/autofs 等持久入口无目标残留；共享和非目标挂载保持可用。 |
| 配置与代理 | 无目标专属配置、符号链接及失效 upstream；若修改共享 Nginx，配置检查通过且其他站点仍正常。 |
| 专属数据 | 目标路径、专属数据库/角色、缓存键空间及部署目录按批准决定处理；保留项与期限独立列出。 |
| 归档 | 每份归档明确保留或获准移除；过期依据、恢复需要和共享内容已核对。与 #217 的归档保留清单分别核对。 |
| 保留验证 | 当前 MSG 服务、worker、托管入口和其他项目可用；受保护材料仍在且权限未改变，不读取或展示 PIN/密钥内容。 |
| 回滚 | 所需备份已验证可用；未获准退役的备份和恢复材料仍保留。 |

部署、只读盘点、写好本页、源码测试通过都不能把这些项目标为已清理。
所有批准对象完成实际复查、保留项通过检查，且未知/失败项处理完毕后，才更新
[#220](https://github.com/TokenNotIncluded/msg/issues/220)；此之前保持开放。
