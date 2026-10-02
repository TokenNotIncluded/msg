# MSG iteration workflow

When iterating this project, the coordinating agent and its subagents must exchange tasks, progress, results, and feedback through the MSG server. Use the agent runtime to start or resume workers; carry substantive coordination through MSG.

- Read the target server's `/AGENTS.md` and applicable rules before writing. Use the configured signed account and the remote mailbox commands (`msg agent ... --remote`); local mailboxes do not satisfy this workflow.
- Give each worker a distinct mailbox label. Keep messages brief: task, current status, evidence or commit, blocker, next action. Never send credentials or private runtime data to a public feedback topic.
- Reuse message IDs for retries. Keep an independent cursor for each reader so that parallel agents do not advance one another's inbox position. Reading does not imply an ACK.
- Record friction discovered during real use in the feedback topic at `/main/msg-self-improvement`. For each improvement, record the observed problem, acceptance criterion, fix, validation, and the result of using MSG again.
- Improve the server or client when actual communication is inconvenient, wastes tokens, or is slow. Preserve authentication, authorization, privacy, and retry semantics. Do not widen the CA or credential scope merely to make a workflow succeed.
- Finish a concrete improvement cycle before starting another: use MSG, collect feedback, implement a bounded fix, validate, release when authorized, and try the workflow again. Report unfinished work honestly.

This workflow is an explicit user requirement. It does not itself authorize unrelated messages, publicity, or changes outside the current task.
