# Contract: PM Agent Python API

`agents/pm_agent.py`. Types `TechnicalSpec`, `Task` and `TokenUsage` come from
`orchestration/state.py` ([../data-model.md](../data-model.md#increment-2-shared-team-state)).
Design: [../research.md](../research.md) R15, R16.

```python
MAX_TASKS = 8
PRIORITIES = typing.get_args(Priority)   # ("high", "medium", "low"), one source of truth

@dataclass
class PMConfig:
    model: str = "claude-sonnet-5-5"   # env PM_MODEL
    effort: str = "high"               # env PM_EFFORT: low | medium | high | xhigh | max
    use_fallbacks: bool = True         # env PM_USE_FALLBACKS
    @classmethod
    def from_env(cls) -> "PMConfig": ...   # raises ValueError on bad values

@dataclass
class PMResult:
    spec: TechnicalSpec | None         # None when rejected or on error
    rejection_reason: str | None       # set when the requirement is not buildable software
    error: str | None                  # API error, refusal, or invalid/unparseable output
    usage: TokenUsage

class PMAgent:
    def __init__(self, config: PMConfig | None = None, client=None) -> None: ...
    def write_spec(self, requirement: str) -> PMResult: ...
    def consolidate(self, spec: TechnicalSpec, requirement: str) -> PMResult: ...

def merge_tasks(tasks: list[Task], limit: int = MAX_TASKS) -> list[Task]: ...
def number_tasks(tasks: list[dict]) -> list[Task]: ...   # assigns T1…Tn by position
def validate_tasks(tasks: list[dict]) -> str | None: ...  # None, or the first problem found
def criteria_preserved(original: list[Task], merged: list[dict]) -> bool: ...
```

Shared request settings come from `agents/llm.py` (`EFFORT_LEVELS`, `MAX_TOKENS`,
`FALLBACK_BETA`, `refusal_message`). `agents/pm_agent.py` does not import
`agents/coder_agent.py`.

- `client` defaults to `anthropic.Anthropic(max_retries=3)`. Any object exposing
  `beta.messages.create(**kwargs)` may be injected (offline tests).
- `PMAgent` needs no Docker. It holds no conversation history: every call stands alone.

## Behavior guarantees

| # | Guarantee |
|---|-----------|
| P1 | `write_spec` raises `ValueError` only for an empty or whitespace-only requirement. Every other failure comes back in `PMResult.error`. |
| P2 | Exactly one of three outcomes: `spec` set (`rejection_reason` and `error` are `None`), `rejection_reason` set, or `error` set. |
| P3 | When `spec` is set: `name`, `description` and `acceptance_criteria` are non-empty; `tasks` has ≥ 1 item; every task has ≥ 1 acceptance criterion and a valid priority; IDs are `T1`…`Tn` in order. `write_spec` does **not** cap the count, because capping is the graph's `consolidate` step. |
| P4 | `consolidate` makes exactly one model call. It returns a spec only if the model's tasks pass `validate_tasks`, number at most `MAX_TASKS`, and satisfy `criteria_preserved` against the input tasks; the tasks are then renumbered. Otherwise it returns `error` naming the failed check, and the caller falls back to `merge_tasks` on the original tasks (P5). A successful result therefore always meets FR-006a. |
| P4a | `criteria_preserved` flattens both lists' acceptance criteria in task order, normalizes each with `" ".join(c.split()).casefold()`, and returns whether the original sequence is a subsequence of the merged one. |
| P5 | `merge_tasks` is pure and deterministic. It returns `min(len(tasks), limit)` tasks, keeps the order, keeps every acceptance criterion, gives each merged task its group's highest priority, and renumbers IDs. |
| P6 | Each call sends one `beta.messages.create` request with structured output (`output_config.format`, JSON Schema), adaptive thinking, the configured effort, and the refusal fallback beta when enabled. No tools are sent. |

## Model-facing output schema (`SPEC_SCHEMA`)

```json
{
  "type": "object",
  "properties": {
    "name": {"type": "string"},
    "description": {"type": "string"},
    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
    "assumptions": {"type": "array", "items": {"type": "string"}},
    "tasks": {"type": "array", "items": {"$ref": "#/$defs/task"}},
    "rejection_reason": {"type": "string"}
  },
  "required": ["name", "description", "acceptance_criteria", "assumptions", "tasks", "rejection_reason"],
  "additionalProperties": false,
  "$defs": {
    "task": {
      "type": "object",
      "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]}
      },
      "required": ["title", "description", "acceptance_criteria", "priority"],
      "additionalProperties": false
    }
  }
}
```

The consolidation call uses `{"tasks": [...]}` with the same task item schema. The model
never writes `task_id`. Array-size rules (≥ 1 item, ≤ 8 tasks) are checked in code,
because structured outputs do not support array-size constraints.
