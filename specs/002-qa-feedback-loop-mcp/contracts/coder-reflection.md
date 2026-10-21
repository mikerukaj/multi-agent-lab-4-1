# Contract: Coder Self-Reflection and Finding Responses

Changes to `agents/coder_agent.py` (research R11, R12). Everything not listed here in
[001 contracts/coder-agent-api.md](../../001-autonomous-dev-team/contracts/coder-agent-api.md)
is unchanged.

## `CoderConfig` additions

| Field | Default | Env | Rule |
|-------|---------|-----|------|
| `reflection_rounds` | 1 | `CODER_REFLECTION_ROUNDS` | 1 to 3, otherwise `ValueError` (FR-127) |
| `sandbox_image` | `SANDBOX_IMAGE` (now the derived pytest image, research R6) | `CODER_SANDBOX_IMAGE` | unchanged |

## Summary schema change (every `run()`)

`SUMMARY_FORMAT` gains one required key. All other keys are unchanged.

```json
"finding_responses": {
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "finding_id": {"type": "string"},
      "action": {"type": "string", "enum": ["fixed", "disputed", "not_addressed"]},
      "reason": {"type": "string"}
    },
    "required": ["finding_id", "action", "reason"],
    "additionalProperties": false
  }
}
```

`SUMMARY_PROMPT` adds: "If the task listed findings (F…) or critique items (C…),
answer each one in `finding_responses`. Leave the list empty otherwise."
`CoderResult.finding_responses: list[dict]` holds the answers. Code drops any ID the task
did not list.

## Tools and MCP

`run()` opens `McpFileTools(workspace, protect=("tests", ".qa"))` for the whole session.
`build_request(config, messages, tools, output_format=None)` now takes the tool list,
which is made once per session from `file_tools.definitions()` plus `EXEC_PYTHON_TOOL`,
each with `purpose`. It is identical on every request in the session. `SYSTEM_PROMPT`
keeps "Do not write test files", and adds: "Writes under `tests/` are refused. You may
read the tests to understand a failure."

## `reflect(brief, workspace) -> ReflectionResult`

```python
@dataclass
class ReflectionResult:
    rounds: list[Reflection]       # data-model.md Reflection (before_iteration is set by the team)
    result: CoderResult            # the last revision's result: the revised output
    status: str                    # result.status
    usage: dict                    # summed over critique and revision calls
```

For each round, `1..config.reflection_rounds`:

1. Read every non-hidden `.py` file outside `tests/` through the session's
   `McpFileTools`.
2. **Critique call**: `beta.messages.create` with `CRITIQUE_SYSTEM_PROMPT`, no tools,
   `output_config = {effort, format: CRITIQUE_FORMAT}`, adaptive thinking, and
   fallbacks. The message holds the brief and every source file in full, each under a
   heading with its path.

   ```text
   CRITIQUE_FORMAT = {items: [{kind: "bug"|"edge_case", location: str,
                               description: str, suggested_change: str}]  (minItems 1),
                      overall: str}
   ```

   Items are numbered `C1…Cn` in code. If the list is empty, the call is retried once. If
   it is still empty, or the call is refused or fails, the round's status is `error`,
   with the reason.
3. **Revision**: `self.run(render_revision_task(brief, items), workspace)`. The task
   lists each item with its ID and asks the Coder to change the code, or to explain why
   no change is needed, then to run the program and answer every `C…` ID in
   `finding_responses`.
4. Record `Reflection(round, critique, revision_status, files_changed, responses,
   summary)`. If the revision's status is `refused` or `error`, stop and return it.
   `step_limit` still counts as a revised output, and the next round, if any, starts.

`reflect` always makes at least one critique call and one revision `run()`. It never
returns the pre-reflection code as its result (planning input: "must run at least once
and produce a revised output").

## Fix rounds

A fix round is a plain `run(render_fix_brief(...), workspace, answer_ids=…)`. Its
layout is in [feedback-loop.md](feedback-loop.md). There is no new method. In a team
run, the `coder` A2A endpoint makes this call when a `fix-program` message arrives from
`qa-debugger` ([a2a-binding.md](a2a-binding.md)). It returns the `CoderFix` as the
`coder-fix` artifact. The build tasks and `reflect` are still called directly by the
team graph, because they are the Coder's own work.

## Standalone CLI

`python -m agents.coder_agent "<task>"` runs `run()`, then `reflect(task, workspace)`
once, and prints `{**CoderResult(revision).to_dict(), "first_draft": <first run
summary>, "reflection": [rounds]}`. The exit-code rules are unchanged and apply to the
revision's status.
