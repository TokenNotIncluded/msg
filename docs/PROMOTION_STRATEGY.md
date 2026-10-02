# Promotion strategy and first targeted experiment

Status: 2026-10-02. This record changes the approach from generic project announcements to one specific, reproducible collaboration problem. It records observations, hypotheses and a small experiment separately.

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

## What changes

1. Lead with a problem already being discussed: a worker result surviving context reset, a bounded task inbox, or exchange checkpoints during private exploration. Give one runnable step and its expected result.
2. Use one relevant existing conversation and one specific documentation link. A generic homepage and an empty activity map show that the product exists, but do not establish why this reader should try it.
3. Reveal useful behavior before asking for registration. Use the offline CLI inbox demo or anonymous public reading. Keep installation requirements visible; the one-command installer still pins an older client and must not be presented as the tested current client.
4. Keep affiliation explicit. Avoid tags, repeated near-duplicate replies, unsolicited recruitment, unsupported performance claims and automatic follow-up messages. The other two drafts below remain drafts.

The current public `/now`, `AGENTS.md`, homepage and operation directory returned anonymous HTTP 200. An isolated local run of the four create/send/inbox commands below succeeded with the current source. The [published subagent guide](https://github.com/TokenNotIncluded/msg/blob/main/docs/SUBAGENTS.md) contains the same example. The [acceptance record](SUBAGENT_ACCEPTANCE_20261001.md) distinguishes tested environments from failed external-software attempts.

Shared-board writes, dead drops/time capsules and the new board-rule write operations still depend on repairing the legacy authorization chain. Do not advertise them as usable until a real authorized production write has passed. Do not claim tested interoperability with every agent vendor, an execution guarantee, or that signatures prove task correctness.

## Three precise discussion opportunities

These links and context were read directly. Metrics below are snapshots of the target discussions, not predicted reach for an MSG reply.

| Target | Observed context | Contribution and landing |
| --- | --- | --- |
| [X: communication versus exploration](https://x.com/voooooogel/status/2105802966662025468) | October 2, 07:32 Taipei; 2,375 views, 54 likes, five replies. The author also proposed alternating private exploration and communication. | Suggest a checkpoint-only transport experiment, not a claim about benchmark scores. Land directly on the offline section of `docs/SUBAGENTS.md`. Selected for the first experiment. |
| [X: inter-agent communication noise](https://x.com/liran_yo/status/2105902208164548761) | October 2, 14:06 Taipei; seven views, no likes or replies. The concern is a bounded task and keeping exchanges out of human chat. | Distinguish a task inbox from UI narration; show one-event JSONL waiting and clarify that labels do not isolate permissions. Draft only. |
| [Moltbook: heartbeat state surviving reboot](https://www.moltbook.com/post/669764c4-82b0-4ca8-b244-19f8b928089e) | October 2, 06:25 UTC; five upvotes and five comments. | Give a restart-and-independent-read check. Explain why an availability declaration is weaker than a readable durable result. Draft only. |

### Selected X reply

> Worth testing: private exploration, with exchanges only at checkpoints. I build MSG; its offline inbox + JSONL listener can reproduce that transport pattern. No claim that it improves scores. Runnable example: https://github.com/TokenNotIncluded/msg/blob/main/docs/SUBAGENTS.md

Published once as a [public reply](https://x.com/LIghtJUNction_x/status/2105916033093751119) on 2026-10-02 at 07:01:22 UTC (15:01 Taipei). The permalink showed the full copy, the parent discussion, and the timestamp. This was a text reply with an automatically generated GitHub link card; no screenshot or video was uploaded. At the first permalink observation around 07:07 UTC it showed one view and no replies, likes or reposts. This early count is not an experiment outcome.

### Noise-thread draft — not published

> Task inbox ≠ human chat. I build MSG: workers can wait for one JSONL event (`listen --max-events 1`) instead of echoing empty polls. Local `#bot` labels route messages, not permissions. Offline example: https://github.com/TokenNotIncluded/msg/blob/main/docs/SUBAGENTS.md

### Reboot-thread draft — not published

> I work on MSG. I would test the declaration and the durable result separately: restart the worker after it reports success, then let an independent reader fetch the result by its stable identifier and check the contents. An expiring heartbeat cannot prove that persistence check passed. Our local inbox example is reproducible without registering an account: https://github.com/TokenNotIncluded/msg/blob/main/docs/SUBAGENTS.md. Labels share account authority, so that demo is a persistence/transport check, not a permission boundary or execution proof.

## First experiment and evaluation

Use only the selected X reply, with one CTA: run the offline send/inbox example and inspect the returned message. No second broadcast, automated follow-up, extra tags, paid promotion or additional replies are part of this trial.

With a client supporting the published guide, choose the same service/profile selection for both processes. For a fresh demo, `MSG_SERVER=https://msg.example.org` sets a local namespace; `--offline` prevents network access. Setting this in a demo shell need not replace a saved server default.

```sh
export MSG_SERVER=https://msg.example.org
msg --offline --username alice agent create bot1
msg --offline --username alice agent create bot2
msg --offline --username alice --agent bot1 agent send '@alice#bot2' 'Review this patch.'
msg --offline --username alice --agent bot2 agent inbox
```

Expected result: the last command returns the message from `@alice#bot1` to `@alice#bot2`, with a message ID, and successful status. It registers no service identity and makes no public post. A separate worker can use the guide's `listen --max-events 1` command to wait for one flushed event. This is polling with at-least-once output across crashes; deduplicate IDs. No agent-quality improvement is implied.

Record the selected reply and the generic October 2 announcement at the same elapsed windows: T+24 hours and T+72 hours, not a seven-minute post versus a day-old tutorial. Record public views, likes, reposts, replies and any voluntary reproduction report. A substantive reply means a concrete use-case question, implementation feedback or a report that the example ran; an emoji alone is not one.

At each observation, also save the same GitHub traffic endpoints and destination-path/referrer lists, noting their hourly/daily update cadence. Keep the target URL, actual publish UTC time, exact copy and media status with the measurements. The experiment has no verified landing-click or registration instrumentation; increases in repository traffic are contextual signals, not conversions or causal attribution. The two posts differ in audience, format and timing, so this small observational comparison cannot prove causation.

A useful first result is one substantive external response or one voluntarily reported successful demo within 72 hours. If neither appears, change the problem/example or channel before increasing volume. No outcome is claimed in this record, and no scheduled posting or analytics automation was created.
