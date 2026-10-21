# Contract: Validating LLM Output Before It Is Used

Module: `orchestration/validation.py`. The reply types are in `orchestration/state.py`.
Research: R10. Directive 4.

## Validator

```text
validate(value: object, schema_type: type) -> list[str]
```

- Returns an empty list when `value` conforms. Otherwise it returns problems as
  `"$.tasks[2].priority: 'urgent' is not one of ('high', 'medium', 'low')"`, in
  document order.
- Supports `TypedDict` (`total`, `Required`, `NotRequired`), `Literal[...]`, `list[T]`,
  `dict[str, T]`, `T | None`, `Union[...]`, `str`, `int` (but not `bool`), `float`
  (accepts `int`), `bool` and `None`.
- A `TypedDict` rejects unknown keys, mirroring `additionalProperties: false`.
- Raises `TypeError` only for an unsupported annotation, which is a programming error
  and is caught by the unit tests, never at run time.

## Reply types and where they are checked

| Call site | JSON schema sent to the model | Reply `TypedDict` | On failure |
|---|---|---|---|
| `PMAgent.write_spec` | `SPEC_SCHEMA` | `PMSpecReply` (`tasks: list[TaskItemReply]`) | `PMResult.error = "Model output failed validation: …"` → `pm_error` |
| `PMAgent.consolidate` | `CONSOLIDATION_SCHEMA` | `PMConsolidationReply` | `"Consolidation rejected: …"` → the existing deterministic merge fallback |
| `CoderAgent._finalize` | `SUMMARY_FORMAT` | `CoderSummaryReply` (`finding_responses: list[FindingResponseReply]`) | the existing tolerant fallback (empty plan, raw text as explanation) + `validation_failed` event; code and execution still come from ground truth |
| `CoderAgent._critique` | `CRITIQUE_FORMAT` | `CritiqueReply` (`items: list[CritiqueItemReply]`, min 1) | status `"error"` → `reflection_failed` |
| `CoderAgent._act` (each `tool_use` block) | `READ_FILE_TOOL` / `WRITE_FILE_TOOL` / `EXEC_PYTHON_TOOL` input schemas | `ReadFileInput` / `WriteFileInput` / `ExecPythonInput` | the tool is not run; an `is_error` tool result `"Invalid tool input: …"`; an unknown tool name → `"Unknown tool: …"` |
| `QAAgent._write_initial_tests`, load repair | `WRITE_TESTS_FORMAT` | `QATestFilesReply` (`test_files: list[TestFileReply]`) | `QAStepError("Malformed QA reply: …")` → `qa_failed` |
| `QAAgent._resolve_disputes` | `DISPUTES_FORMAT` | `QADisputeReply` | same |
| `QAAgent._review` | `REVIEW_FORMAT` | `QAReviewReply` | same |
| `ModelCaller` cached replay (R6) | the original call's schema | the same reply type as the live call | the cache entry is discarded and the `DependencyFailure` is returned |

Order of checks in every structured call:
`stop_reason` (refusal) → join text → `json.loads` → `validate` → existing semantic
checks → use. Semantic checks are unchanged:

- tasks are valid;
- criteria are preserved in order;
- there are at most 8 tasks after consolidation;
- `same_as` names a known finding;
- `finding_index` is in range;
- paths are under `tests/`;
- criteria come from the spec;
- every failing test is covered.

## Schema ⇄ TypedDict agreement (enforced by test)

`tests/test_validation.py::SchemaAgreementTests` checks each JSON schema against its
reply `TypedDict`. For each object level it checks:

- property names equal the `TypedDict` keys;
- `required` equals the keys that are not `NotRequired`;
- every `enum` equals the `Literal` args;
- `type: array` ↔ `list`, `type: integer` ↔ `int`, `type: string` ↔ `str`.

Changing either side without the other fails the suite.

## Events

Every failure emits `validation_failed` (severity `warning`, or `error` when it ends the
run). The details are `agent`, `reply_type` and `problems` (the first 5, redacted).
