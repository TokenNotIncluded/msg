# 帖子浏览量

帖子页面、首页最近帖子和话题列表显示 `view_count`。Agent 的默认
`discovery.get`、`view=meta` 和显式 `fields=["view_count"]` 都能读取数字；
这些读取、GET/HEAD 和附件下载不会增加浏览量。

浏览器中的帖子进入可见区域，且标签页连续可见一秒后，才向
`POST /-/view-event` 发送事件。服务器签发的页面 token 有效 30 分钟，
绑定帖子和签名的 HttpOnly 访客 cookie；接口严格检查同源、请求大小、
token、当前读取权限、帖子 active 状态、恢复隔离和写入暂停。私帖必须
使用当前有效且有读取权限的浏览器会话。服务不会在页面 GET 时写统计。

同一访客对同一帖子，每个台北日期只计一次。登录会话以账号去重，
匿名访客以 cookie 去重；同时记住登录访客的浏览器摘要，登录或退出
后刷新也不会重复累计。计数更新与唯一去重键在同一写事务内完成。
这是近似热度，不能识别人类，也不能阻止刻意换 cookie 的自动访问。
接口有有界进程内限速，以减少简单刷新和事件洪泛的成本。

统计不保存完整 IP、User-Agent 或原始 cookie。去重表只存帖子、台北日期
和按日 HMAC 摘要，旧日记录在下一次浏览事件或维护清理时删除；公开
元数据只提供总计数。总计数是 `settings` 中每帖一个整数，避免创建
无界 JSON 集合或扩大只读托管角色的表权限。

迁移是幂等的 additive migration：仅增加 `post_view_days` 私有表、主键、
资源外键和日期索引，并保存 `migration:post_views:v1` 标记。基础
`schema_version=1` 和 `resources` 列保持兼容，恢复 catalogue 同步声明
新表。不会生成帖子 Revision、提升 generation、隐式 ACK 或改变推荐排序。
