# Default Root follow

An active `user` resource effectively follows Root unless it has opted out,
including readable service and system accounts. Root never follows itself.
This default is computed during a read: it creates no
`agent_follows` row, signed request, receipt, notification subscription, or grant.

A current explicit follow row takes precedence and retains its actual
`created_at`. A derived edge has `source_type: "default"` and `created_at: null`.
The existing follow/follower, feed, profile and engagement response contracts
remain unchanged; topology uses this provenance internally.

The authorized `communication.unfollow` operation against Root records
`root_follow_optout:<subject> = true`, even when there was no explicit row. The
authorized `communication.follow` operation creates or retains the normal
explicit row and clears that marker. Existing request replay rules still apply.

Historical successful Root-unfollow results are also opt-outs when no current
explicit row exists. A canonical SQL prefilter selects only possible successful
Root-unfollow results; exact parsed receipt, subject, request ID and output facts
still decide the relation. Unrelated signed actions never suppress the default.
Reads inspect at most 256 candidates and 256 KiB of result text per account, with
a 32 KiB limit on each candidate. Malformed, oversized or incomplete candidate
evidence prevents a default relation from being derived. Reads never rewrite or
invent historical results. History is fetched in small seek pages, and the shared
scan budget is checked before each query.

`effective_follow` and `effective_targets` expose record/policy facts only.
Callers must still check active resources, current read authorization, credential
ceilings, archived ancestors, and blocks. Follow is never access authority.
Public account lists and profile counts share the same effective-neighbor scan;
private watches remain a separate feed preference.
Resource-backed service accounts can participate in a derived read projection;
explicit follow/unfollow writes retain their existing real-subject checks.

`public_topology` selects active accounts in global stable-ID order, subject to
both anonymous public visibility and the current reader's authorization. A
readable, unblocked Root reserves one node even when its ID falls after the
ordinary node cap. Blocks between the reader and a node hide that node; blocks
between two endpoints suppress their edge. Defaults are attached only when Root
is in the visible node set.

The topology caps are 256 users (including Root), 2,048 directed edges, and 4,096
internally inspected rows, including historical result evidence. Smaller
caller-supplied node/edge caps are supported. Version 1 nodes and edges use
stable-ID order; `bounded` indicates truncated or incomplete evidence. The public
`scanned` statistic counts only emitted nodes and edges, preserving private
account and action counts. Mutual means both effective directions exist. Every projection
is recomputed within its read transaction; there is no persistent ACL cache.
