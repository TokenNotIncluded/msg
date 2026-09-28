# Tool execution boundary

Refs #160, #82, #85. This is a module-boundary change, not a new tool authority or scheduler.

Before: EffectWorker injected an untyped callable; BubblewrapRunner imported ToolResult from EffectWorker's implementation module. The unused core ToolExecutor.invoke declaration instead claimed to return a published ResourceRef.

After:

```text
EffectWorker --> core.tool_execution.ToolRunner / ToolResult
BubblewrapRunner --> core.tool_execution.ToolResult
EffectWorker -- trusted default construction --> BubblewrapRunner
runner -- local ToolResult --> worker validation/current authority/attempt fence
worker -- existing storage transaction --> published ResourceRef
```

ToolRunner is the actual asynchronous callable port: ToolSpec, arguments, the tuple of already intersected NetworkPolicy alternatives and a per-attempt output directory. ToolResult is only a frozen local-artifact record (path, media_type, metadata). It is not proof of authorization, successful publication, or exactly-once execution. Runtime validation, mandatory isolation, output bounds, quarantine, current credential/certificate checks and attempt/state/deadline fences remain unchanged in their existing owners.

EffectWorker's constructor and BubblewrapRunner's callable now share explicit input/output annotations; the assembly tests compare the annotations and exercise a correctly injected runner in real PostgreSQL. Returning an existing ResourceRef instead of a local artifact cannot bypass the publisher; the attempt becomes uncertain and no output resource is created.

Compatibility: `msg.workers.effects.ToolResult` still exports the exact same class, not a copy or adapter. `msg.core.contracts.ToolExecutor` remains an import alias for ToolRunner. Its unused `invoke(...) -> ResourceRef` declaration is retired; no in-repository implementation or caller used it. This preserves the old import spelling, not that never-implemented invocation interface. New callers should use ToolRunner. No SMTP/Webhook/Git backend is made to implement this tool-specific port.

Evidence lives in `tests/test_tool_runner_contract.py`, existing `tests/test_tools.py` and `tests/test_effect_completion_fencing.py`, the focused cloud workflow and the full CI gate. A missing-bwrap refusal test is not a claim of successful real bubblewrap or production acceptance.
