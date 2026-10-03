# Historical promotion experiment · 2026-10-02

This dated record preserves the observed baseline and one published reply.
It is not current product, installer or authorization guidance. Current workflow
examples are in [subagents](../SUBAGENTS.md); original environment evidence is in
[October 1 acceptance](20261001-subagent-acceptance.md). Unpublished drafts and
prospective posting plans were removed; this archive authorizes no follow-up.

## Observed baseline

The following are snapshots, not a complete history or a conversion report. X and Moltbook observations were collected around 06:55 UTC; GitHub was checked at 06:55:31 UTC. New posts need the same elapsed observation window before comparison.

### X: public metrics

The [account](https://x.com/LIghtJUNction_x) showed 17 followers. The inspected MSG posts were:

| Post | Published | Views | Likes | Replies | Reposts |
| --- | --- | ---: | ---: | ---: | ---: |
| [Illustrated terminal introduction](https://x.com/LIghtJUNction_x/status/2105389700534137061) | 2026-10-01 04:09 Taipei | 46 | 2 | 0 | 0 |
| [Long CLI quickstart](https://x.com/LIghtJUNction_x/status/2105447278521790685) | 2026-10-01 07:58 Taipei | 67 | 1 | 0 | 0 |
| [Pinned constellation introduction](https://x.com/LIghtJUNction_x/status/2105734618205081682) | approximately 11 hours earlier | 77 | 2 | 0 | 1 |
| [New public-activity introduction](https://x.com/LIghtJUNction_x/status/2105912548285673781) | 2026-10-02 14:47 Taipei | 3 | 0 | 0 | 0 |

The new post was only about seven minutes old. It is too early to label it a failure. The account had liked the illustrated introduction and had liked/reposted the pinned post; these counts include the account's own actions and are not net new audience engagement. Views are platform counts, not unique people, link clicks or registrations. No creator link-click analytics were read.

The visible profile also mixed agent introductions with general Linux tutorials. That is an audience-positioning hypothesis, not proof that the mix caused low reach.

### Moltbook: public sample

The [existing agent profile](https://www.moltbook.com/u/lightjunctioncodex) showed six followers and karma 28. Its returned `recentPosts` contained eight posts from September 25, all in `m/general`, published within about 7.5 hours. They totaled 21 upvotes and ten comments; medians were three upvotes and one comment, and three posts had no comments. This returned sample does not include every post, and the profile did not yet include the October 2 post.

The [new infrastructure post](https://www.moltbook.com/post/533c39e9-7ebf-4c6f-9f88-b181fc8ee4e7) had two upvotes and no comments about twelve minutes after publication. The API reported `verified`, `is_spam=false`, and not deleted; the public page showed Verified. No click, landing, registration or attribution data were read.

Historical public examples: [identity continuity](https://www.moltbook.com/post/3476a629-2884-4ce4-b744-f7bddf4ca8d8) had 4 upvotes / 0 comments; [repository use case](https://www.moltbook.com/post/e2b2dfec-1223-48bd-bf9e-77f6ccca0b23) had 3 / 3; [trust chain](https://www.moltbook.com/post/a5e99ba7-03d4-4f1f-b5fb-355d7d74c62d) had 3 / 2. This is insufficient to rank positioning variants causally.

### GitHub: available traffic API

All four repository traffic endpoints were readable. Returned daily UTC buckets covered September 18 through October 1, inclusive.

| Metric | Count | GitHub-reported window uniques |
| --- | ---: | ---: |
| Views | 261 | 22 |
| Full clones | 23,593 | 3,968 |
| `github.com` referrer | 22 | 4 |
| `linux.sb` referrer | 6 | 4 |

No X or Moltbook referrer appeared in the returned popular-referrer list. These lists are not exhaustive attribution: absence does not prove that no visit occurred. Popular paths still included the old repository name: old Overview 84 / 14, old pulls 45 / 1; current `msg` Overview was 3 / 3.

Clones and GitHub uniques do not establish human users, agent adoption, registrations or campaign impact. Do not sum daily uniques to construct interval uniques. Preserve snapshots before the rolling 14-day window expires. [GitHub traffic API definitions](https://docs.github.com/en/rest/metrics/traffic) specify the window and update cadence.

## Published reply

> Worth testing: private exploration, with exchanges only at checkpoints. I build MSG; its offline inbox + JSONL listener can reproduce that transport pattern. No claim that it improves scores. Runnable example: https://github.com/TokenNotIncluded/msg/blob/main/docs/SUBAGENTS.md

Published once as a [public reply](https://x.com/LIghtJUNction_x/status/2105916033093751119) on 2026-10-02 at 07:01:22 UTC (15:01 Taipei). The permalink showed the full copy, the parent discussion, and the timestamp. This was a text reply with an automatically generated GitHub link card; no screenshot or video was uploaded. At the first permalink observation around 07:07 UTC it showed one view and no replies, likes or reposts. This early count is not an experiment outcome.

## Interpretation

The initial observation does not establish an experiment outcome. No verified
landing-click, registration or causal attribution data were collected, and no
scheduled posting or analytics automation was created. A later comparison would
require matching elapsed observation windows and separately recorded evidence.
