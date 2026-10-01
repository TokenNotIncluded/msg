"""Readable permission guide, available as browser HTML and raw Markdown."""

PERMISSIONS_MARKDOWN = """# Permission bits / 权限位说明

A mode such as `2770` describes who can read, write and enter a resource.
`2770` 这样的四位数字，用来说明谁可以读取、写入和进入资源。
点击频道表里的数字，可以查看该频道实际的 owner（所有者）、group（用户组）和 mode（权限位）。

## Read the four digits / 四位数字怎么读

From left to right: **special flags · owner · group · everyone else**.
从左到右：**特殊标记 · 所有者 · 所属用户组 · 其他人**。数字使用八进制，每位范围是 0–7。

For each of the last three digits, add these permissions:
后三位分别把下面的权限相加：

| Value / 数值 | Meaning / 含义 |
| ---: | --- |
| 4 | Read / 读取；目录中也用于列出可读条目 |
| 2 | Write / 写入；具体发布、编辑、移除仍由相应操作检查 |
| 1 | Traverse / 进入目录、访问其下资源；不表示能在网页执行代码 |
| 0 | No permission / 没有这些权限 |

所以 `7 = 4+2+1`（读、写、进入），`6 = 4+2`（读、写），`5 = 4+1`（读、进入）。

**Select one class, not the sum of all three.** An owner uses the owner digit;
an active member of the resource's group uses the group digit; others use the last digit.
**只选择一类，不把后三位合并。** 你是资源所有者，就看所有者位；否则是该资源用户组的有效成员，
就看组权限位；其他人看最后一位。加入 `&admins` 不等于变成所有资源的所有者。

## Special flags / 第一位的特殊标记

The first digit also adds flags together. / 第一位也可以把标记相加。

| Value / 数值 | Meaning in MSG / MSG 中的含义 |
| ---: | --- |
| 4 | Certificate gate / 写入证书门槛：写操作还需要当前有效、范围匹配的能力证书；普通权限位不能绕过它，单纯读取不因这一位要求证书 |
| 2 | Inherit group / 新建子资源继承这个目录的用户组 |
| 1 | Protect removals / 移除或移出子资源时，还要求你是子资源所有者或父目录所有者 |
| 0 | No extra flag / 没有以上标记 |

## Common examples / 常见例子

| Mode / 权限位 | Explanation / 解释 |
| --- | --- |
| `2770` | `/admins`：所有者与管理员组可读、写、进入，其他人没有权限；新建子资源继承管理员组 |
| `1777` | 三类都有读、写、进入权限；但移除或移出别人的子资源受第一位保护 |
| `5777` | 在 `1777` 的基础上，再加证书门槛（第一位 `5 = 4+1`） |
| `0555` | 三类都可读、进入，普通权限位不允许写入；例如遗嘱频道还有专门的签署流程 |
| `0700` | 只有所有者可读、写、进入；管理员组成员不因此获得读取权限 |
| `0660` | 所有者和用户组可读、写；其他人无权限，常用于组内帖子 |

## Why a mode is not the whole decision / 为什么有权限位还可能被拒绝

MSG checks the current identity, active group membership, ancestor directories,
credential scope, certificate gates and the requested operation together.
频道可见性还受祖先目录权限、当前身份、有效组成员关系和凭据范围影响。
发帖还可能要求加入话题、未被禁言，以及适用的证书。模式允许写入，不代表每种写操作都会通过。
浏览器登录只授权读取；写操作仍由客户端签名。

The channel table lists readable discussion channels for the current visitor.
Anonymous visitors see public channels; signed-in visitors also see authorized private channels.
首页频道表按当前访问者的权限显示：未登录时显示公开频道，登录后补上有权读取的私有频道。
私聊会话在收件箱和私聊入口展示，不混入频道表。

[Home / 首页](/) · [Sign in / 登录](/login) · [Platform security rules / 安全规则](/_rules/security) · [Topic rules / 话题规则](/_rules/topics)
"""
