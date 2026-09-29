# SearchQuery @5

`discovery.lexical_search@5` 扩展未发布的新查询契约；@1–@4 的输入 schema 保留。
HTTP query、`/_search/q/5/`（短别名 `/_s/q/5/`）、签名操作和 sealed QueryRef
使用同一查询 handler 与当前授权。新增操作版本仍受调用凭据的版本上限约束。

## 范围与过滤

`scope` 可以是原有路径/ID，或下列闭合 JSON 对象之一：

- `{"subject":"u_..."}`：该用户目录的后代及 owner/group 指向该用户的资源。
- `{"org":"g_..."}`：该组织目录的后代及 owner/group 指向该组织的资源。
- `{"resource_refs":[{"id":"r_..."},{"id":"r_..."}]}`：最多 32 个当前资源范围的并集。

重复解析到同一资源的范围去重；深度和递归限制继续适用。当前根范围必须可访问，
每个结果仍独立鉴权；续页重新检查范围与结果权限。这里不接受固定历史 Revision
作为范围，也不会把组织范围当作成员自动取得读取权限的依据。

新增 `revision` 与 `source_version` 过滤当前 Revision；旧版本不会因为符合过滤而被重新展示。
`source_version` 范围为 1–2147483647。`field=title` 与显式 `fields=["title"]`
使用 Resource.name，包含其原有文件后缀；不从 Markdown 推测标题。

`relation_to` 筛选指向指定资源的结果，`relation_from` 筛选由指定资源引用的结果；
`relation_type` 可以进一步限制关系类型。`has_replies` 表示存在可读的当前 `reply_to`
入边，`has_references` 表示存在可读的当前入边。两者均接受 true/false。
过滤关系时，两端都必须当前可读；历史关系、撤权端点不进入命中或统计。
@5 还接受协作资源登记的 `reference/state/target/content` 关系类型。

## 显式拼写提示

`spell=true` 增加 `spelling` 字段；默认没有该字段，提示不改写查询、不改变结果项。
词典仅来自当前范围、结构过滤与权限检查之后的可读资源名称。
最多 512 个词，每词最多 32 字符；查询最多 4 个词，各为 2–32 字符。
按 Levenshtein 距离、出现次数、词序确定排序，每词最多返回 5 个提示。
查询词长度不超过 4 时距离上限为 1，否则为 2。超出预算明确失败。
这不是全文纠错、语义搜索或恒定时间保证。

## CLI 与纯路径

```sh
msg search /main gadget --field title --spell --facet type --fields id,title
msg search '{"resource_refs":[{"id":"r_one"},{"id":"r_two"}]}' needle --limit 10
msg search /main needle --relation-to r_target --relation-type reply_to
msg search /main needle --source-kind release --source-version 1
msg search --cursor '<server-issued-cursor>'
```

CLI 仅检查有界 cursor 的公开查询参数来选择操作版本；不把该检查当作验签或授权。
服务端仍核验 MAC、调用主体、过期时间和当前权限。cursor 不可与新筛选条件混用。

@5 纯路径新增 `rv`（revision）、`sv`（source_version）、`to/fr`（关系端点）、
`hr/hf`（has_replies/has_references）、`sp`（spell）。title 字段短值是 `t`。
JSON scope 放在 `s` 段，按现有路径规范编码（逗号保留）；较长输入使用 QueryRef。
完整映射可从 `/-/d/search.query` 读取。
