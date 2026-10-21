# Contract: QA & Debugger Agent and the Sandboxed Test Runner

Implemented in `agents/qa_agent.py` and `tools/test_runner.py`. Types are from
[data-model.md](../data-model.md). The design is in [research.md](../research.md) R7–R10
and R14.

## `QAConfig`

```python
@dataclass
class QAConfig:
    model: str = "claude-sonnet-5-5"     # QA_MODEL
    effort: str = "high"                 # QA_EFFORT, one of EFFORT_LEVELS
    test_timeout_seconds: float = 60.0   # QA_TEST_TIMEOUT, > 0
    repair_attempts: int = 2             # QA_REPAIR_ATTEMPTS, 0-3
    use_fallbacks: bool = True           # QA_USE_FALLBACKS
    sandbox_image: str = SANDBOX_IMAGE   # CODER_SANDBOX_IMAGE (one image for both agents)

    @classmethod
    def from_env(cls) -> "QAConfig": ...  # ValueError on bad values
```

## `QAAgent`

```python
class QAAgent:
    def __init__(
        self,
        config: QAConfig | None = None,
        client=None,                  # anthropic.Anthropic(max_retries=3) by default
        run_tests=run_pytest,         # (workspace, timeout_seconds, image) -> TestRun
        file_tools=McpFileTools,      # (workspace, protect=()) -> context manager
    ) -> None: ...                    # calls ensure_sandbox(image); SandboxUnavailableError

    def run_iteration(self, context: QAContext) -> QAIterationResult: ...
    def final_report(self, request: FinalReportRequest) -> QAReport: ...
```

`QAContext` and `FinalReportRequest` are JSON-only `TypedDict`s in
`orchestration/state.py` ([data-model.md](../data-model.md), "QA request payloads"), so
they cross the A2A broker unchanged. `run_iteration` turns `context["workspace"]` (a
string) into a `Path` itself.

In a team run, nothing calls these methods directly. The `qa-debugger` A2A endpoint
([a2a-binding.md](a2a-binding.md)) calls them on its broker worker thread:
- **`review-program`**: it passes the message's `data` part through as the `QAContext`,
  and returns `result.to_dict()` as the `qa-iteration` artifact.
- **`final-report`**: it passes the `data` part through as the `FinalReportRequest`.

The endpoint never imports this module (SC-110). It only relies on these two method
names and on the result's `error` attribute and `to_dict()`.

`run_tests` and `file_tools` can be swapped out, so the tests can give QA mocked code
input without Docker (research R15). Neither method raises for model, tool or MCP
failures. These come back in `QAIterationResult.error`.

### `QAContext` (input)

A JSON-only `TypedDict` in `orchestration/state.py`, built by the team's `qa` node. The
fields and rules are defined once, in [data-model.md](../data-model.md) → "QA request
payloads" → QAContext. `workspace` is an absolute path **string**.

### `QAIterationResult` (output)

| Field | Type | Notes |
|-------|------|-------|
| `test_run` | TestRun | |
| `findings` | list[QAFinding] | normalised. Must validate against [qa-findings.schema.json](qa-findings.schema.json) |
| `disputes` | list[Dispute] | |
| `test_changes` | list[TestChange] | |
| `test_suite` | dict[str, str] | QA's updated copy of the suite |
| `verdict` | Verdict | worked out in code (research R10) |
| `usage` | TokenUsage | |
| `error` | str \| None | set when QA could not finish its turn. `verdict` is then `fix_needed`, and the team ends the run with `qa_failed` |

### Steps inside `run_iteration`

Order and conditions are in research R8. Every file read and write in these steps goes
through one `McpFileTools(workspace)` session opened for the iteration.

1. **Check the suite** (iteration ≥ 2). If a file on disk differs from
   `context.test_suite`, or is missing, it is written back (`TestChange` with
   reason `restored`). One blocking `bug` finding is added: "test files were changed
   outside QA: <paths>".
2. **`write_tests`** (iteration 1). QA deletes the existing contents of `tests/`, then
   makes one model call with `WRITE_TESTS_FORMAT`. The prompt contains the spec (all
   acceptance criteria), `interface_summary(sources)` (no function bodies, FR-102a), the
   Coder's notes and the main file. It never contains the full source.
3. **`resolve_disputes`** (iteration ≥ 2, only if any response is `disputed`). One call
   with `DISPUTES_FORMAT`. `correct` decisions come with replacement files.
4. **Write and run**. QA writes every changed file through MCP `write_file`, checking
   that each path is under `tests/`, then calls `run_tests(...)`.
5. **`repair_tests`**. While `not test_run.loaded`, QA makes up to `repair_attempts`
   calls with `WRITE_TESTS_FORMAT`, giving the load-error output, and runs the tests again
   after each. If the suite still doesn't load, `error = "QA test suite failed to load:
   <first line>"`.
6. **`review`**. One call with `REVIEW_FORMAT`. QA reads the full source through MCP
   only at this point.
7. **Normalise** (research R10). Regression tests are written through MCP, recorded as
   `TestChange` with reason `regression`, and run from the next iteration on.

### Model-facing output schemas

All are `output_config.format = {"type": "json_schema", "schema": …}`, and every
object has `additionalProperties: false` with all its keys required. Empty strings
stand for "none", as `SPEC_SCHEMA.rejection_reason` does in the PM.

```text
TEST_FILE      = {path: str, content: str, criteria: [str]}
WRITE_TESTS_FORMAT = {test_files: [TEST_FILE], notes: str}
DISPUTES_FORMAT    = {decisions: [{finding_id: str, decision: "keep"|"correct", reason: str}],
                      corrected_files: [TEST_FILE]}
REVIEW_FORMAT      = {findings: [{kind: "failing_test"|"bug"|"style",
                                  rule: "" | <CONSTITUTION_RULES>,
                                  file: str, location: str, problem: str, expected: str,
                                  likely_cause: str, fix_instruction: str,
                                  test_ids: [str], same_as: str}],
                      regression_tests: [{finding_index: int, path: str, content: str,
                                          criteria: [str]}],
                      summary: str}
```

`REVIEW_FORMAT` has no `severity` field, because code sets severity (FR-104a). `same_as`
is an earlier `finding_id`, or `""`.

**Rejected replies**. Any of the following counts as a malformed reply, and QA ends
its turn with an error:
- a test path outside `tests/`;
- a `criteria` entry that is not in the spec;
- `finding_index` out of range;
- `same_as` naming an unknown ID;
- in iteration 1, an empty `test_files` list.

### QA prompts (summary of `QA_SYSTEM_PROMPT`)

The system prompt tells QA:
- QA owns `tests/` and writes pytest tests (plain `assert`, `pytest.raises`,
  `pytest.mark.parametrize`, `capsys`/`monkeypatch` for CLI behavior). The tests use
  only pytest and the standard library.
- Expected values come only from the spec, never from the code.
- Name each test after the acceptance criterion it checks.
- Report every failing test.
- Mark code that special-cases test inputs as a `bug` (FR-111a).
- Use `style` only for the listed constitution rules. Anything else is advisory.
- Never ask questions.

## `final_report(request: FinalReportRequest) -> QAReport`

`FinalReportRequest` is a `TypedDict` in `orchestration/state.py`, built by the team's
`finish` node from `TeamState`. The fields and rules are defined in
[data-model.md](../data-model.md) → "QA request payloads" → FinalReportRequest.

Deterministic, with no model call (research R14). It writes `.qa/report.md` through
MCP. The Markdown has a heading with the result and end reason, a counts line, an
"Unresolved findings" list (ID, file:location, problem, fix instruction), an
"Advisory" list, and the per-iteration lines.

## `tools/test_runner.py`

```python
def run_pytest(workspace: Path, timeout_seconds: float, image: str = SANDBOX_IMAGE) -> TestRun
def parse_junit(xml_text: str, test_files: list[str]) -> list[TestCaseResult]
def pytest_command(workspace: Path, out_dir: Path, name: str, image: str) -> list[str]
```

The `docker run` flags are the same as `tools/python_exec.sandbox_command`, plus a few
extras:
- `--network none`
- `--read-only` with a `/tmp` tmpfs
- `--cap-drop ALL`
- `no-new-privileges`
- 512 MB memory, 1 CPU and 128 processes
- `--user` set to the host user
- no environment variables apart from `HOME`, `PYTHONIOENCODING` and
  `PYTHONDONTWRITEBYTECODE`

Extras:
- `--volume <ws>:/work:rw`
- `--volume <ws>/tests:/work/tests:ro`
- `--volume <tmp out_dir>:/qa-out:rw`

The command is
`python -s -m pytest tests -q -p no:cacheprovider -c /dev/null --rootdir=/work
--confcutdir=/work/tests --junitxml=/qa-out/junit.xml -o junit_family=xunit2`.

Results:

| Case | `TestRun` |
|------|-----------|
| exit 0, XML parsed, every QA test file has ≥ 1 case | `loaded=True`, counts from XML |
| exit 1 | `loaded=True`, counts from XML |
| exit 2/3/4/5, XML missing or > 2 MB or unparsable, or a test file with no cases | `loaded=False`, `output` holds the error text |
| exceeds `timeout_seconds` | container killed, `timed_out=True`, `exit_code=None`, `loaded=True` (a timeout is a test failure, not a load error). Any partial XML is used. If there is none, `cases=[]` and `failed=1`. Normalising adds a blocking `failing_test` finding with `test_ids=["<timeout>"]` |
| Docker unavailable | `SandboxUnavailableError` (raised, as `ensure_sandbox` does now) |
